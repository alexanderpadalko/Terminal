"""Shared Finam-only history, stream reconciliation and calculation cache."""
from __future__ import annotations
from collections import OrderedDict
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
from .finam_api import FinamAPI, FRAMES, DEPTH_DAYS, symbol_name, parse_instant
from .market_data import Candle, normalize, candle_close_time
from .pipeline import analyze_timeframes
from .finam_auction import exclude_stock_closing_auction
from .settings import IndicatorSettings

TTL_SECONDS = {"M1":60,"M5":300,"H1":300,"H4":900,"D1":1800,"W1":3600,"MN1":14400}
STREAM_VERIFY_SECONDS = 900
FULL_VERIFY_SECONDS = 86400

class FinamMarketService:
    def __init__(self, path=None, *, api=None, clock=None):
        self.path = Path(path or Path.home()/".flowdesk"/"finam_candles_v1.sqlite3")
        self.api = api or FinamAPI()
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.latest_stream_at = {}
        self.last_snapshot = {}
        self.analysis_cache = OrderedDict()
        self._pull_locks = {}
        self._analysis_locks = {}
        self._stream_updates = {}
        self._repair = set()
        self._quotes = {}
        self._failures = {}
        self._repair_tried = {}
        self._generation = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=30)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL, tf TEXT NOT NULL, ts TEXT NOT NULL,
                o REAL,h REAL,l REAL,c REAL,v REAL,closes_at TEXT,
                PRIMARY KEY(symbol,tf,ts))""")
            db.execute("""CREATE TABLE IF NOT EXISTS finam_pulls (
                symbol TEXT NOT NULL,tf TEXT NOT NULL,pulled TEXT NOT NULL,
                full_due TEXT NOT NULL,PRIMARY KEY(symbol,tf))""")
            db.execute("""CREATE TABLE IF NOT EXISTS finam_history_progress (
                symbol TEXT NOT NULL,tf TEXT NOT NULL,cursor TEXT NOT NULL,
                exhausted INTEGER NOT NULL,PRIMARY KEY(symbol,tf))""")

    def _connection(self):
        return sqlite3.connect(self.path,timeout=30)

    def _key_lock(self,locks,key):
        with self.lock:
            return locks.setdefault(key,threading.Lock())

    @staticmethod
    def _acquire(lock,cancelled):
        while not lock.acquire(timeout=.1):
            if cancelled():raise InterruptedError()
        if cancelled():
            lock.release()
            raise InterruptedError()

    def _upsert(self,symbol,timeframe,bars):
        if not bars:return False
        with self.lock,closing(self._connection()) as db,db:
            db.executemany("""INSERT INTO candles
                (symbol,tf,ts,o,h,l,c,v,closes_at) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,tf,ts) DO UPDATE SET
                o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,
                v=excluded.v,closes_at=excluded.closes_at
                WHERE candles.o IS NOT excluded.o OR candles.h IS NOT excluded.h
                OR candles.l IS NOT excluded.l OR candles.c IS NOT excluded.c
                OR candles.v IS NOT excluded.v OR candles.closes_at IS NOT excluded.closes_at""",
                ((symbol,timeframe,b.time.isoformat(),b.open,b.high,b.low,b.close,
                  b.volume,b.scheduled_close.isoformat() if b.scheduled_close else None) for b in bars))
            changed=bool(db.total_changes)
            if changed:
                for key in list(self.analysis_cache):
                    if key[0]==symbol:self.analysis_cache.pop(key,None)
            return changed

    def _series(self,symbol):
        with closing(self._connection()) as db:
            rows=db.execute("""SELECT tf,ts,o,h,l,c,v,closes_at FROM candles
                WHERE symbol=? ORDER BY ts""",(symbol,)).fetchall()
        series={tf:[] for tf in FRAMES}
        for tf,ts,o,h,l,c,v,ending in rows:
            if tf in series:
                series[tf].append(Candle(datetime.fromisoformat(ts),o,h,l,c,v,
                    datetime.fromisoformat(ending) if ending else None))
        return {tf:normalize(bars) for tf,bars in series.items() if bars}

    def invalidate_network_cache(self):
        with self.lock,closing(self._connection()) as db,db:
            self._generation+=1
            self._quotes.clear()
            self._failures.clear()
            self._repair_tried.clear()
            self._stream_updates.clear()
            self.analysis_cache.clear()
            db.execute("DELETE FROM finam_pulls")

    def _tail(self,symbol,tf,limit=3):
        with closing(self._connection()) as db:
            rows=db.execute("""SELECT ts,o,h,l,c,v,closes_at FROM candles
                WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?""",(symbol,tf,limit)).fetchall()
        return [Candle(datetime.fromisoformat(ts),o,h,l,c,v,
                       datetime.fromisoformat(end) if end else None)
                for ts,o,h,l,c,v,end in reversed(rows)]

    def mark_stream_reconnect(self,symbol):
        with self.lock:
            self._repair.update((symbol,tf) for tf in FRAMES)
            for tf in FRAMES:
                self._stream_updates.pop((symbol,tf),None)
                self._repair_tried.pop((symbol,tf),None)

    def ingest_quote(self,event):
        if event.kind!="quote" or not isinstance(event.quote,dict):return
        try:stamp=parse_instant(event.quote.get("timestamp"))
        except (TypeError,ValueError,KeyError,OverflowError):return
        now=self.clock()
        observed=now-timedelta(minutes=self.api.market_delay_minutes(event.board))
        if not -5<=(observed-stamp).total_seconds()<=90:return
        symbol=symbol_name(event.symbol,event.board)
        with self.lock:self._quotes[symbol]=(now,{"symbol":symbol,**event.quote})

    def ingest_stream(self,ticker,mic,events):
        symbol=symbol_name(ticker,mic)
        for event in events:
            if (event.kind!="candles" or
                (event.symbol.upper(),event.board.upper())!=(ticker.upper(),mic.upper()) or
                event.timeframe not in FRAMES or not event.candles):continue
            prior=self._tail(symbol,event.timeframe,1)
            first=min(c.time for c in event.candles)
            if prior and first>candle_close_time(prior[-1],event.timeframe):
                with self.lock:
                    self._repair.add((symbol,event.timeframe))
                    self._repair_tried.pop((symbol,event.timeframe),None)
            self._upsert(symbol,event.timeframe,event.candles)
            with self.lock:
                self.latest_stream_at[symbol]=max(self.latest_stream_at.get(symbol,event.received_at),event.received_at)
                self._stream_updates[(symbol,event.timeframe)]=event.received_at

    def _stream_fresh(self,symbol,tf,bars,now):
        with self.lock:received=self._stream_updates.get((symbol,tf))
        if not received or not bars or not 0<=(now-received).total_seconds()<=90:return False
        observed=now-timedelta(minutes=self.api.market_delay_minutes(symbol.rsplit("@",1)[1]))
        return bars[-1].time<=observed<candle_close_time(bars[-1],tf)

    def refresh_series(self,symbol,frames,*,priority=0,cancelled=lambda:False):
        """Deduplicate refreshes by exact symbol/TF; do not lock the DB across HTTP."""
        errors={}
        for tf in frames:
            if cancelled():raise InterruptedError()
            key=(symbol,tf)
            lock=self._key_lock(self._pull_locks,key)
            self._acquire(lock,cancelled)
            try:
                now=self.clock()
                bars=self._tail(symbol,tf)
                with closing(self._connection()) as db:
                    row=db.execute("SELECT pulled,full_due FROM finam_pulls WHERE symbol=? AND tf=?",key).fetchone()
                pulled,full_due=(tuple(datetime.fromisoformat(v) for v in row) if row
                                 else (None,now+timedelta(seconds=FULL_VERIFY_SECONDS)))
                healthy=self._stream_fresh(symbol,tf,bars,now)
                ttl=max(TTL_SECONDS[tf],STREAM_VERIFY_SECONDS) if healthy else TTL_SECONDS[tf]
                with self.lock:
                    repair=key in self._repair
                    generation=self._generation
                    failure=self._failures.get(key)
                    tried=self._repair_tried.get(key)
                if failure and 0<=(now-failure[0]).total_seconds()<60:
                    errors[tf]=failure[1]
                    continue
                if repair and tried and 0<=(now-tried).total_seconds()<30:continue
                if pulled and not repair and 0<=(now-pulled).total_seconds()<ttl:
                    if not bars:errors[tf]="Finam не вернул свечи"
                    continue
                full=not bars or now>=full_due
                kwargs={}
                if bars and not full:
                    kwargs={"start":max(now-timedelta(days=DEPTH_DAYS[tf]),bars[-min(3,len(bars))].time),"end":now}
                    if kwargs["start"]>=now:
                        errors[tf]="Finam: некорректное время свечи"
                        continue
                scope=getattr(self.api,"request_scope",None)
                try:
                    with scope(priority=priority,cancelled=cancelled) if scope else nullcontext():
                        fetched=self.api.bars(symbol,tf,**kwargs)
                    if cancelled():raise InterruptedError()
                    with self.lock:
                        if generation!=self._generation:raise InterruptedError()
                        self._repair_tried[key]=now
                        self._failures.pop(key,None)
                        if fetched:self._upsert(symbol,tf,fetched)
                        elif not bars:errors[tf]="Finam не вернул свечи"
                        due=now+timedelta(seconds=FULL_VERIFY_SECONDS) if full else full_due
                        with closing(self._connection()) as db,db:
                            db.execute("INSERT OR REPLACE INTO finam_pulls VALUES(?,?,?,?)",(*key,now.isoformat(),due.isoformat()))
                        if fetched:self._repair.discard(key)
                except InterruptedError:raise
                except Exception as exc:
                    code=getattr(exc,"http_status",None)
                    if code==429:raise
                    errors[tf]={401:"ошибка авторизации Finam (401)",403:"нет доступа Finam (403)",
                        404:"контракт не найден Finam (404)"}.get(code,"загрузка Finam недоступна")
                    with self.lock:self._failures[key]=(now,errors[tf])
            finally:lock.release()
        return errors

    def latest_quote(self,symbol,*,cancelled=lambda:False):
        now=self.clock()
        with self.lock:
            cached=self._quotes.get(symbol)
            if cached and 0<=(now-cached[0]).total_seconds()<30:return cached[1]
        if cancelled():raise InterruptedError()
        scope=getattr(self.api,"request_scope",None)
        with scope(priority=10,cancelled=cancelled) if scope else nullcontext():
            result=self.api.latest_quote(symbol)
        with self.lock:self._quotes[symbol]=(now,result)
        return result

    def analyze_cached(self,symbol,timeframe,series,settings,observed):
        lock=self._key_lock(self._analysis_locks,(symbol,timeframe))
        with lock:
            # All OHLCV and scheduled closes participate, including historical corrections.
            fingerprint=tuple((tf,tuple(bars),sum(b.time<=observed for b in bars),
                               sum(candle_close_time(b,tf)<=observed for b in bars))
                              for tf,bars in sorted(series.items()))
            key=(settings.calculation_key(),fingerprint)
            cache_id=(symbol,timeframe)
            with self.lock:cached=self.analysis_cache.get(cache_id)
            if cached and cached[0]==key and cached[1][0].asof<=observed:return cached[1]
            mic=symbol.rsplit("@",1)[1]
            delay=self.api.market_delay_minutes(mic)
            provenance={tf:{"mode":"FINAM_NATIVE_CANDLES","symbol":symbol,"market":mic,
                "native_higher":tf in ("D1","W1","MN1"),"feed_delay_seconds":delay*60} for tf in series}
            result=analyze_timeframes(series,timeframe,asof=observed,settings=settings,provenance=provenance)
            with self.lock:
                self.analysis_cache[cache_id]=(key,result)
                self.analysis_cache.move_to_end(cache_id)
                while len(self.analysis_cache)>48:self.analysis_cache.popitem(last=False)
            return result

    def load_history_progress(self):
        with closing(self._connection()) as db:
            rows=db.execute("SELECT symbol,tf,cursor,exhausted FROM finam_history_progress").fetchall()
        return {(s,tf):{"cursor":datetime.fromisoformat(t),"exhausted":bool(done)} for s,tf,t,done in rows}

    def save_history_progress(self,symbol,tf,progress):
        with self.lock,closing(self._connection()) as db,db:
            db.execute("INSERT OR REPLACE INTO finam_history_progress VALUES(?,?,?,?)",
                (symbol,tf,progress["cursor"].isoformat(),int(progress["exhausted"])))

    def view(self,ticker,mic,timeframe,*,refresh=False,allow_http=False,settings=None,
             progress=None,cancelled=lambda:False,**ignored):
        symbol=symbol_name(ticker,mic)
        if timeframe not in FRAMES:raise ValueError("Неизвестный таймфрейм Finam")
        settings=settings or IndicatorSettings()
        progress=progress or (lambda message:None)
        errors={}
        if allow_http:
            progress("Finam: сверка последних свечей")
            errors=self.refresh_series(symbol,(timeframe,),cancelled=cancelled)
            if self._tail(symbol,timeframe,1):
                errors.update(self.refresh_series(symbol,(tf for tf in FRAMES if tf!=timeframe),cancelled=cancelled))
        series=exclude_stock_closing_auction(self._series(symbol),mic)
        if not series.get(timeframe):
            reason=errors.get(timeframe,"нет сохранённых свечей. Разрешите HTTP для первоначальной загрузки")
            raise RuntimeError(f"Finam · {symbol} · {timeframe}: {reason}")
        delay=self.api.market_delay_minutes(mic)
        observed=self.clock()-timedelta(minutes=delay)
        progress("Finam: расчёт потоков v2.11")
        main,parents=self.analyze_cached(symbol,timeframe,series,settings,observed)
        with self.lock:
            prior=self.latest_stream_at.get(symbol)
            repair=any((symbol,tf) in self._repair for tf in FRAMES)
        return main,parents,{
            "symbol":ticker,"board":mic,"tf":timeframe,
            "tier":"stream" if prior else ("network" if allow_http else "disk"),
            "asof":main.asof.isoformat(),"requested_at":self.clock().isoformat(),
            "stream_received_at":prior.isoformat() if prior else None,"feed_delay_seconds":delay*60,
            "warning":"Полный паритет Pine на других биржах не подтверждён"+
                (". Не обновлены: "+"; ".join(f"{tf}: {reason}" for tf,reason in errors.items()) if errors else ""),
            "stream_repair_required":repair}
