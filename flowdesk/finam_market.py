"""Native candle storage/reconciliation, instantiated separately per provider."""
from __future__ import annotations
from collections import OrderedDict
from contextlib import closing, nullcontext, contextmanager
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
import sqlite3
import threading
import math
from .finam_api import FinamAPI, FinamAPIError, FRAMES, DEPTH_DAYS, parse_instant
from .terminal_policy import (DISPLAY_TIMEFRAMES, SOURCE_TIMEFRAMES, LEGACY_SOURCE_TIMEFRAMES,
                              FLOW_TIMEFRAMES, POLICY_VERSION, HTTP_REFRESH_SECONDS)
from .market_symbols import symbol_name
from .market_data import Candle, normalize, candle_close_time
from .pipeline import analyze_timeframes
from .finam_auction import exclude_stock_closing_auction
from .settings import IndicatorSettings
from . import session_store
from .trading_calendar import calendar_for
from .provider_candles import native_higher_enabled, NATIVE_HIGHER
from .us_futures_family import product as session_future
from .moex_trading_family import enabled as moex_trading, PROGRESS as MOEX_PROGRESS
from . import moex_trading_family as moex
from .calculation_store import CalculationStore
from .chart_source_cache import ChartSourceCache, read_frames
from .cancellation import Cancelled, checkpoint, is_cancelled, scope as cancellation_scope
from .session_candles import ClosedBucketCache

TTL_SECONDS = {"M1":60,"M5":300,"H1":300,"H4":900,"D1":1800,"W1":3600,"MN1":14400}
STREAM_VERIFY_SECONDS = HTTP_REFRESH_SECONDS
FULL_VERIFY_SECONDS = 86400
ANALYSIS_CACHE_ENTRIES = 4
ANALYSIS_CACHE_UNITS = 100000


class _ObservationRows:
    def __init__(self, frames):
        self.frames = tuple(rows for _, rows, _ in frames.values())

    def __iter__(self):
        return chain.from_iterable(self.frames)


class _ChartPreparation:
    def __init__(self):
        self.lock = threading.Lock()
        self.revision = None
        self.buckets = ClosedBucketCache()


class FinamMarketService:
    @property
    def provider_name(self):return getattr(self.api,"provider_name","Finam")
    def __init__(self, path=None, *, api=None, clock=None,calendar_factory=None):
        self.path = Path(path or Path.home()/".flowdesk"/"finam_candles_v1.sqlite3")
        self.api = api or FinamAPI()
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.calendar_factory=calendar_factory or calendar_for
        self.lock = threading.RLock()
        self._history_locks = {}
        self.latest_stream_at = {}
        self.last_snapshot = {}
        self.analysis_cache = OrderedDict()
        self.calculations=CalculationStore(self.path.with_name(self.path.stem+'_analysis_v1.sqlite3'))
        self._active_analysis_key = None
        self._chart_sources = ChartSourceCache()
        self._chart_preparation = None
        self._source_repair_attempts = {}
        self._source_repair_checked = {}
        self._pull_locks = {}
        self._analysis_locks = {}
        self._stream_updates = {}
        self._repair = set()
        self._quotes = {}
        self._failures = {}
        self._repair_tried = {}
        self._generation = 0
        self._scan_local=threading.local()
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
            db.execute('''CREATE TABLE IF NOT EXISTS full_archive_turns (
                symbol TEXT NOT NULL,tf TEXT NOT NULL,turns INTEGER NOT NULL,
                PRIMARY KEY(symbol,tf))''')
            session_store.initialize(db)

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

    def _upsert(self,symbol,timeframe,bars,*,source='http'):
        if not bars:return False
        with self.lock,closing(self._connection()) as db,db:
            if source=='stream' and getattr(self.api,'session_candles',False):
                observed=self.clock()-timedelta(minutes=self.api.market_delay_minutes(symbol.rsplit('@',1)[1]))
                accepted=[]
                for bar in bars:
                    saved=db.execute('''SELECT c.o,c.h,c.l,c.c,c.v,o.observed_at FROM candles c
                        JOIN candle_source_observations o USING(symbol,tf,ts)
                        WHERE c.symbol=? AND c.tf=? AND c.ts=?''',
                        (symbol,timeframe,bar.time.isoformat())).fetchone()
                    ending=session_store.source_end(self,symbol,timeframe,bar)
                    if saved and ending<=observed and datetime.fromisoformat(saved[5])>=ending:
                        if tuple(saved[:5])!=(bar.open,bar.high,bar.low,bar.close,bar.volume):
                            self._repair.add((symbol,timeframe));self._repair_tried.pop((symbol,timeframe),None)
                        continue
                    accepted.append(bar)
                bars=accepted
                if not bars:return False
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
            if getattr(self.api,'session_candles',False):
                observed=self.clock()-timedelta(minutes=self.api.market_delay_minutes(symbol.rsplit('@',1)[1]))
                revised=session_store.changed(db,symbol,timeframe,bars,observed,replace=changed or source=='http')
                if revised and symbol==self._chart_sources.symbol and timeframe in self._source_frames():
                    start=end=None
                    if all(b.time.utcoffset()==timedelta(0) for b in bars):
                        start=min(b.time for b in bars).isoformat()
                        end=max(b.time for b in bars).isoformat()
                    self._commit_source_change(db,symbol,timeframe,start,end)
            # Fingerprints validate corrections on the next calculation.
            # Keep unchanged TF/block results available for selective reuse.
            return changed

    def _commit_source_change(self,db,symbol,frame=None,start=None,end=None):
        # The service lock covers both the SQLite commit and the in-memory
        # record. A reader can never see an uncommitted delta in its input.
        revision=db.execute('SELECT revision FROM candle_source_revisions WHERE symbol=?',(symbol,)).fetchone()[0]
        db.commit()
        self._chart_sources.record(symbol,revision,frame,start,end)

    def _source_frames(self):
        # Production uses native Finam M5/H1/D/W/M; historical fakes
        # can still exercise archived session families offline.
        return (SOURCE_TIMEFRAMES if getattr(self.api,'native_all_frames',False)
                else LEGACY_SOURCE_TIMEFRAMES)

    def _series(self,symbol,*,chart=False):
        snapshot=getattr(self._scan_local,'snapshot',None)
        if chart and snapshot is None and getattr(self.api,'session_candles',False):
            return self._chart_series(symbol)
        frames=self._source_frames()
        series={tf:[] for tf in frames}
        placeholders=','.join('?' for _ in frames)
        with closing(self._connection()) as db:
            db.execute('BEGIN')
            rows=db.execute("""SELECT tf,ts,o,h,l,c,v,closes_at FROM candles
                WHERE symbol=? AND tf IN ("""+placeholders+""") ORDER BY ts""",(symbol,*frames))
            for index,(tf,ts,o,h,l,c,v,ending) in enumerate(rows):
                if index % 1024 == 0:
                    checkpoint()
                if tf in series:
                    series[tf].append(Candle(datetime.fromisoformat(ts),o,h,l,c,v,
                        datetime.fromisoformat(ending) if ending else None))
            state=None
            if getattr(self.api,'session_candles',False):
                state=self._source_state(db,symbol)
                state['observations']=db.execute('''SELECT tf,ts,observed_at FROM candle_source_observations
                    WHERE symbol=? AND tf IN ('''+placeholders+')',(symbol,*frames)).fetchall()
        result={tf:normalize(bars) for tf,bars in series.items() if bars}
        if snapshot is not None:
            # HTTP responses own the refreshed suffix even if a selected-chart
            # WebSocket update arrived in the shared DB during this scan.
            for (name,tf),(start,bars) in snapshot.items():
                if name!=symbol:continue
                prior=[b for b in result.get(tf,()) if start is not None and b.time<start]
                result[tf]=normalize([*prior,*bars])
        return session_store.SourceSeries(result,state) if state is not None else result

    @staticmethod
    def _source_state(db,symbol):
        row=db.execute('SELECT revision FROM candle_source_revisions WHERE symbol=?',(symbol,)).fetchone()
        return {'revision':row[0] if row else 0,
                'coverage':db.execute('SELECT tf,start,ending FROM candle_source_coverage WHERE symbol=?',(symbol,)).fetchall(),
                'moex_progress':db.execute('SELECT cursor,exhausted FROM finam_history_progress WHERE symbol=? AND tf=?',
                                           (symbol,MOEX_PROGRESS)).fetchone(),
                'full_archive':bool(db.execute("SELECT 1 FROM finam_history_progress WHERE symbol=? AND tf GLOB 'FULL_ARCHIVE_v2_*' LIMIT 1",
                                               (symbol,)).fetchone())}

    def _chart_series(self,symbol):
        with closing(self._connection()) as db:
            db.execute('BEGIN')
            state=self._source_state(db,symbol)
            revision=state['revision']
            with self.lock:
                saved,changes=self._chart_sources.inputs(symbol,revision)
                preparation=self._chart_preparation if self._chart_sources.symbol==symbol else None
            frames=read_frames(db,symbol,saved,changes,source_frames=self._source_frames())
        checkpoint()
        with self.lock:
            if preparation is self._chart_preparation:
                self._chart_sources.publish(symbol,revision,frames)
        state['observations']=_ObservationRows(frames)
        raw=session_store.SourceSeries({tf:data[0] for tf,data in frames.items() if data[0]},state)
        raw.chart_preparation=preparation
        return raw

    def _select_chart_sources(self,symbol):
        if symbol!=self._chart_sources.symbol:
            self._chart_sources.select(symbol)
            self._chart_preparation=_ChartPreparation() if symbol is not None else None

    def release_chart_sources(self):
        with self.lock:
            self._select_chart_sources(None)

    @contextmanager
    def http_scan(self):
        previous=getattr(self._scan_local,'snapshot',None)
        self._scan_local.snapshot={}
        try:yield
        finally:self._scan_local.snapshot=previous

    def prepare_series(self,symbol,raw,observed,*,display_tf=None):
        # Active native sources retain all provider OHLCV including stock auctions.
        direct=(bool(getattr(self.api,'h1_only_h4_native_dwm',False)) or
                bool(getattr(self.api,'native_all_frames',False)))
        cleaned=raw if direct else exclude_stock_closing_auction(raw,symbol.rsplit('@',1)[1])
        if hasattr(raw,'source_state'):
            cleaned=session_store.SourceSeries(cleaned,raw.source_state)
        preparation=getattr(raw,'chart_preparation',None)
        if preparation is None:
            return session_store.prepare(self,symbol,cleaned,observed)
        revision=raw.source_state['revision']
        self._acquire(preparation.lock,is_cancelled)
        try:
            changes=None
            with self.lock:
                if preparation is self._chart_preparation and preparation.revision is not None:
                    changes=self._chart_sources.changes_since(symbol,preparation.revision,revision)
            ranges=None
            if changes is not None and all(start is not None and end is not None for _,_,start,end in changes):
                ranges=tuple((frame,datetime.fromisoformat(start),datetime.fromisoformat(end))
                             for _,frame,start,end in changes)
            # An interrupted or failed preparation must revalidate the bucket cache on retry.
            preparation.revision=None
            result=session_store.prepare(self,symbol,cleaned,observed,
                                         bucket_cache=preparation.buckets,bucket_changes=ranges)
            preparation.revision=revision
            return result
        finally:
            preparation.lock.release()

    def repair_source_evidence(self,symbol,raw,observed,*,cancelled=lambda:False,priority=0):
        from .source_repair import repair_source_evidence
        return repair_source_evidence(self,symbol,raw,observed,cancelled=cancelled,priority=priority)

    def invalidate_network_cache(self):
        with self.lock,closing(self._connection()) as db,db:
            self._generation+=1
            self._quotes.clear()
            self._failures.clear()
            self._repair_tried.clear()
            self._stream_updates.clear()
            self.analysis_cache.clear()
            self._select_chart_sources(None)
            self._source_repair_attempts={}
            self._source_repair_checked={}
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
            frames=self.api.stream_frames if native_higher_enabled(self.api) else self._source_frames()
            frames=tuple(tf for tf in frames if tf in self._source_frames())
            self._repair.update((symbol,tf) for tf in frames)
            for tf in frames:
                self._stream_updates.pop((symbol,tf),None)
                self._repair_tried.pop((symbol,tf),None)

    def stream_repair_required(self,symbol):
        now=self.clock()
        with self.lock:
            for key in self._repair:
                if key[0]!=symbol:
                    continue
                tried=self._repair_tried.get(key)
                failed=self._failures.get(key)
                if tried and 0<=(now-tried).total_seconds()<30:
                    continue
                if failed and 0<=(now-failed[0]).total_seconds()<60:
                    continue
                return True
        return False

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
                event.timeframe not in self._source_frames() or not event.candles):continue
            prior=self._tail(symbol,event.timeframe,1)
            first=min(c.time for c in event.candles)
            if prior and first>candle_close_time(prior[-1],event.timeframe):
                with self.lock:
                    self._repair.add((symbol,event.timeframe))
                    self._repair_tried.pop((symbol,event.timeframe),None)
            self._upsert(symbol,event.timeframe,event.candles,source='stream')
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
        native_all=bool(getattr(self.api,'native_all_frames',False))
        direct=bool(getattr(self.api,'h1_only_h4_native_dwm',False))
        if direct and not native_all:
            frames=tuple('H1' if tf=='H4' else tf for tf in frames)
        frames=tuple(tf for tf in frames if tf in self._source_frames())
        policy=bool(getattr(self.api,'session_candles',False))
        if policy:
            # US futures need H1 for session OHLCV; RU futures need it for
            # continuous Open. Preserve native archives separately.
            needed=[]
            for frame in frames:
                if native_all or direct:
                    sources=(frame,)
                elif native_higher_enabled(self.api):
                    sources=(('H1','M5') if frame=='H4' else
                             ('H1',frame) if session_future(symbol) and frame in NATIVE_HIGHER else
                             ('H1','D1',frame) if moex_trading(symbol) and frame in NATIVE_HIGHER else (frame,))
                else:
                    sources=('H1','M5','D1') if frame in ('H4',*NATIVE_HIGHER) else (frame,)
                for item in sources:
                    if item not in needed:needed.append(item)
            frames=needed
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
                snapshot=getattr(self._scan_local,'snapshot',None)
                healthy=snapshot is None and self._stream_fresh(symbol,tf,bars,now)
                ttl=max(TTL_SECONDS[tf],STREAM_VERIFY_SECONDS) if healthy else TTL_SECONDS[tf]
                with self.lock:
                    repair=key in self._repair
                    generation=self._generation
                    failure=self._failures.get(key)
                    tried=self._repair_tried.get(key)
                with closing(self._connection()) as db:
                    covered=db.execute('SELECT 1 FROM candle_source_coverage WHERE symbol=? AND tf=? LIMIT 1',key).fetchone()
                if failure and 0<=(now-failure[0]).total_seconds()<60:
                    errors[tf]=failure[1]
                    continue
                if repair and tried and 0<=(now-tried).total_seconds()<30:continue
                if snapshot is None and pulled and not repair and not (policy and not covered) and 0<=(now-pulled).total_seconds()<ttl:
                    if not bars:errors[tf]="Finam не вернул свечи"
                    continue
                full=not bars or now>=full_due or (policy and not covered)
                kwargs={}
                if bars and not full:
                    kwargs={"start":max(now-timedelta(days=DEPTH_DAYS[tf]),bars[-min(3,len(bars))].time),"end":now}
                    if kwargs["start"]>=now:
                        errors[tf]="Finam: некорректное время свечи"
                        continue
                if policy and not kwargs:
                    depth=getattr(self.api,'candle_depth_days',DEPTH_DAYS)[tf]
                    kwargs={'start':now-timedelta(days=depth),'end':now}
                scope=getattr(self.api,"request_scope",None)
                try:
                    with scope(priority=priority,cancelled=cancelled) if scope else nullcontext():
                        fetched=self.api.bars(symbol,tf,**kwargs)
                    if snapshot is not None:
                        beginning=kwargs.get('start') or min((b.time for b in fetched),default=None)
                        snapshot[(symbol,tf)]=(beginning,tuple(fetched))
                    if cancelled():raise InterruptedError()
                    with self.lock:
                        if generation!=self._generation:raise InterruptedError()
                        self._repair_tried[key]=now
                        self._failures.pop(key,None)
                        if fetched:self._upsert(symbol,tf,fetched)
                        elif not bars:errors[tf]="Finam не вернул свечи"
                        if policy:
                            observed=now-timedelta(minutes=self.api.market_delay_minutes(symbol.rsplit('@',1)[1]))
                            session_store.record_response(self,symbol,tf,fetched,kwargs['start'],kwargs['end'],observed)
                        due=now+timedelta(seconds=FULL_VERIFY_SECONDS) if full else full_due
                        with closing(self._connection()) as db,db:
                            db.execute("INSERT OR REPLACE INTO finam_pulls VALUES(?,?,?,?)",(*key,now.isoformat(),due.isoformat()))
                        if fetched:self._repair.discard(key)
                except InterruptedError:raise
                except Exception as exc:
                    from .diagnostics_log import log_event
                    log_event('candle_source_unavailable',symbol=symbol,timeframe=tf,
                              error=exc,stage='candles',outcome='failed')
                    code=getattr(exc,"http_status",None)
                    if code==429:raise
                    errors[tf]={401:"ошибка авторизации Finam (401)",403:"нет доступа Finam (403)",
                        404:"контракт не найден Finam (404)"}.get(code,"загрузка Finam недоступна")
                    with self.lock:self._failures[key]=(now,errors[tf])
            finally:lock.release()
        return {tf:reason.replace("Finam",self.provider_name) for tf,reason in errors.items()}

    def latest_quote(self,symbol,*,cancelled=lambda:False):
        now=self.clock()
        with self.lock:
            cached=self._quotes.get(symbol)
            if getattr(self._scan_local,'snapshot',None) is None and cached and 0<=(now-cached[0]).total_seconds()<30:return cached[1]
        if cancelled():raise InterruptedError()
        scope=getattr(self.api,"request_scope",None)
        with scope(priority=10,cancelled=cancelled) if scope else nullcontext():
            result=self.api.latest_quote(symbol)
        with self.lock:self._quotes[symbol]=(now,result)
        return result

    def analyze_cached(self,symbol,timeframe,series,settings,observed,*,terminal=True):
        """Active product scope by default; terminal=False is offline reference only."""
        if terminal and timeframe not in DISPLAY_TIMEFRAMES:
            raise ValueError('Таймфрейм недоступен в терминале')
        lock=self._key_lock(self._analysis_locks,(symbol,timeframe))
        self._acquire(lock,is_cancelled)
        try:
            # All OHLCV and scheduled closes participate, including historical corrections.
            needed=set(FLOW_TIMEFRAMES if terminal else ('H1','H4','D1','W1','MN1'))
            if timeframe in ('M1','M5'):needed.add('M5')
            if timeframe=='M1':needed.add('M1')
            fingerprints=[]
            for tf,bars in sorted(series.items()):
                if tf not in needed:
                    continue
                opened=closed=0
                for index,bar in enumerate(bars):
                    if index % 1024 == 0:
                        checkpoint()
                    opened+=bar.time<=observed
                    closed+=candle_close_time(bar,tf)<=observed
                fingerprints.append((tf,tuple(bars),opened,closed))
            fingerprint=tuple(fingerprints)
            session_meta=getattr(series,'session_metadata',{})
            cache_id=(symbol,timeframe)
            with self.lock:cached=self.analysis_cache.get(cache_id)
            if cached is None and terminal:
                cached=self.calculations.load(symbol,timeframe,cancelled=is_cancelled)
            getter=getattr(self.api,'cached_tick_size',None)
            try:tick=getter(symbol) if callable(getter) else None
            except Exception:tick=None  # Metadata failure cannot hide price history.
            if not isinstance(tick,(int,float)) or isinstance(tick,bool) or not math.isfinite(tick) or tick<=0:
                tick=getattr(cached[1][0],'minimum_tick',.01) if cached else .01
            key=(settings.calculation_key(),fingerprint,repr(session_meta),
                 (POLICY_VERSION if terminal else 'reference',float(tick)))
            checkpoint()
            if cached and cached[0]==key and cached[1][0].asof<=observed:
                with self.lock:
                    self.analysis_cache[cache_id]=cached
                    self.analysis_cache.move_to_end(cache_id)
                    self._trim_analysis_cache()
                return cached[1]
            mic=symbol.rsplit("@",1)[1]
            delay=self.api.market_delay_minutes(mic)
            provenance={tf:{"mode":getattr(self.api,"provenance_mode","FINAM_NATIVE_CANDLES"),"symbol":symbol,"market":mic,
                "native_higher":tf in ("D1","W1","MN1"),"feed_delay_seconds":delay*60,
                "minimum_tick":float(tick)} for tf in series}
            if session_meta:
                for tf in provenance:
                    provenance[tf].update(session_meta)
                    if tf in ('H1','H4','D1','W1','MN1'):
                        native=session_meta.get('origins',{}).get(tf)=='finam_native'
                        provenance[tf]['mode']='FINAM_NATIVE_CANDLES' if native else 'SESSION_CANDLES'
                        provenance[tf]['native_higher']=native
            previous=None
            if (cached and cached[0][0]==key[0] and cached[0][-1]==key[-1] and
                    getattr(cached[1][0],'session_info',{}).get('history_breaks',{})==session_meta.get('history_breaks',{})):
                previous={cached[1][0].timeframe:cached[1][0],**cached[1][1]}
            result=analyze_timeframes(series,timeframe,asof=observed,settings=settings,
                                      provenance=provenance,cached=previous,terminal=terminal)
            checkpoint()
            if terminal:
                self.calculations.save(symbol,timeframe,key,result,cancelled=is_cancelled)
            checkpoint()
            with self.lock:
                self.analysis_cache[cache_id]=(key,result)
                self.analysis_cache.move_to_end(cache_id)
                self._trim_analysis_cache()
            return result
        finally:
            lock.release()

    def _trim_analysis_cache(self):
        """Bound retained calculation objects, never trim stored/chart candles."""
        def units(result):
            pending=[result[0],*result[1].values()];seen=set();total=0
            while pending:
                item=pending.pop()
                if id(item) in seen:continue
                seen.add(id(item))
                total+=len(item.candles)+len(getattr(item,'flows',()))+2*len(getattr(item,'liquidity_audit',()))
                pending.extend(getattr(item,'summary_sources',{}).values())
            return total
        sizes={key:units(value[1]) for key,value in self.analysis_cache.items()}
        while len(sizes)>ANALYSIS_CACHE_ENTRIES or sum(sizes.values())>ANALYSIS_CACHE_UNITS:
            victim=next((key for key in sizes if key!=self._active_analysis_key),None)
            if victim is None:break  # One active large chart still retains its entire history.
            self.analysis_cache.pop(victim,None);sizes.pop(victim)

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
        try:
            with cancellation_scope(cancelled):
                return self._view(ticker,mic,timeframe,allow_http=allow_http,settings=settings,
                                  progress=progress,cancelled=cancelled)
        except Cancelled as exc:
            raise InterruptedError() from exc

    def _view(self,ticker,mic,timeframe,*,allow_http,settings,progress,cancelled):
        symbol=symbol_name(ticker,mic)
        if timeframe not in DISPLAY_TIMEFRAMES:raise ValueError("Таймфрейм недоступен в терминале")
        with self.lock:
            self._active_analysis_key=(symbol,timeframe)
            self._select_chart_sources(symbol)
        settings=settings or IndicatorSettings()
        progress=progress or (lambda message:None)
        errors={}
        if allow_http:
            progress(self.provider_name+": сверка последних свечей")
            errors=self.refresh_series(symbol,(timeframe,),cancelled=cancelled)
            if self._tail(symbol,timeframe,1) or self._tail(symbol,'M5',1):
                errors.update(self.refresh_series(symbol,(tf for tf in SOURCE_TIMEFRAMES if tf!=timeframe),cancelled=cancelled))
        delay=self.api.market_delay_minutes(mic)
        observed=self.clock()-timedelta(minutes=delay)
        raw=self._series(symbol,chart=True)
        if (allow_http and not getattr(self.api,'native_all_frames',False) and
                'H1' not in errors and
                self.repair_source_evidence(symbol,raw,observed,cancelled=cancelled)):
            raw=self._series(symbol,chart=True)
            observed=self.clock()-timedelta(minutes=self.api.market_delay_minutes(mic))
        if (allow_http and not getattr(self.api,'native_all_frames',False)
                and not getattr(self.api,'h1_only_h4_native_dwm',False)
                and timeframe in NATIVE_HIGHER and native_higher_enabled(self.api)
                and moex_trading(symbol) and (not moex.archive_ready(raw,symbol)
                    or not moex.opening_ready(raw,symbol,observed))):
            # This runs in the selected chart's cancellable worker at chart
            # priority. First opening must not depend on a 30-symbol scanner
            # finishing before its low-priority archive pass even starts.
            progress('Finam: подготовка дневной истории торговых дней и цен открытия')
            try:moex.backfill(self,symbol,self.clock(),cancelled=cancelled)
            except InterruptedError:raise
            except Exception as exc:
                code=getattr(exc,'http_status',None)
                reason={401:'нужно обновить авторизацию Финама',403:'нет доступа к исходной истории Финама',
                        404:'контракт не найден Финамом',429:'достигнут лимит запросов Финама'}.get(
                            code if type(code) is int else None,'не удалось подготовить исходные свечи Финама')
                raise FinamAPIError(reason,http_status=code if type(code) is int and 100<=code<=599 else None) from None
            raw=self._series(symbol,chart=True)
            observed=self.clock()-timedelta(minutes=delay)
        series=self.prepare_series(symbol,raw,observed,display_tf=timeframe)
        if not series.get(timeframe):
            issue=getattr(series,'session_metadata',{}).get('issues',{}).get(timeframe)
            reason=("нет полных исходных свечей или подтверждённого календаря сессии" if issue
                    else errors.get(timeframe,"нет сохранённых свечей. Разрешите HTTP для первоначальной загрузки"))
            if moex_trading(symbol):
                reason={
                    'incomplete_daily_coverage':('подготовка дневной истории Финама ещё не завершена'
                        if not moex.archive_ready(raw,symbol) else 'дневная история Финама не покрывает нужные периоды'),
                    'native_snapshot_unconfirmed':'Финам ещё не подтвердил снимок исходной свечи',
                    'opening_source_unavailable':'Финам ещё не подтвердил первую цену непрерывных торгов',
                    'opening_source_conflict':'исходные цены открытия Финама требуют повторной сверки',
                    'calendar_history_unknown':'нет подтверждённого календаря для дат исходной истории',
                    'calendar_source_conflict':'дата исходной свечи противоречит календарю торгов',
                }.get(issue,reason)
            raise RuntimeError(f"{self.provider_name} · {symbol} · {timeframe}: {reason}")
        progress("Finam: расчёт потоков v2.11")
        main,parents=self.analyze_cached(symbol,timeframe,series,settings,observed,terminal=True)
        with self.lock:
            prior=self.latest_stream_at.get(symbol)
            repair=any((symbol,tf) in self._repair for tf in FRAMES)
        return main,parents,{
            "symbol":ticker,"board":mic,"tf":timeframe,"provider":self.provider_name,
            "tier":"stream" if prior else ("network" if allow_http else "disk"),
            "asof":main.asof.isoformat(),"requested_at":self.clock().isoformat(),
            "stream_received_at":prior.isoformat() if prior else None,"feed_delay_seconds":delay*60,
            "warning":"Полный паритет Pine на других биржах не подтверждён"+
                (". Не обновлены: "+"; ".join(f"{tf}: {reason}" for tf,reason in errors.items()) if errors else ""),
            "stream_repair_required":repair}
