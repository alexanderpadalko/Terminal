"""Finam-only candle cache and v2.11 calculation bridge.

All state is provider-scoped: never read/merge old MOEX ISS SQLite archives.
REST is gated by explicit allow_http. Token renewal is transport authentication,
not permission to download market history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
from contextlib import closing

from .finam_api import FinamAPI, FRAMES, symbol_name
from .market_data import Candle, normalize, candle_close_time
from .pipeline import analyze_timeframes
from .finam_auction import exclude_stock_closing_auction
from .settings import IndicatorSettings


class FinamMarketService:
    def __init__(self, path=None, *, api=None, clock=None):
        self.path = Path(path or Path.home() / ".flowdesk" / "finam_candles_v1.sqlite3")
        self.api = api or FinamAPI()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.latest_stream_at = {}
        self.last_snapshot = {}
        self.analysis_cache = {}

    def _connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.execute("""CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL, tf TEXT NOT NULL, ts TEXT NOT NULL,
                o REAL, h REAL, l REAL, c REAL, v REAL, closes_at TEXT,
                PRIMARY KEY(symbol, tf, ts))""")
        return con

    def _upsert(self, symbol, timeframe, bars):
        if not bars:
            return
        with closing(self._connection()) as db, db:
            db.executemany("""INSERT OR REPLACE INTO candles
                (symbol,tf,ts,o,h,l,c,v,closes_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                ((symbol,timeframe,b.time.isoformat(),b.open,b.high,b.low,b.close,
                  b.volume,b.scheduled_close.isoformat() if b.scheduled_close else None)
                 for b in bars))

    def _series(self, symbol):
        with closing(self._connection()) as db, db:
            rows = db.execute("""SELECT tf,ts,o,h,l,c,v,closes_at FROM candles
                WHERE symbol=? ORDER BY ts""", (symbol,)).fetchall()
        series = {tf: [] for tf in FRAMES}
        for tf, ts, o, h, l, c, v, ending in rows:
            if tf in series:
                series[tf].append(Candle(datetime.fromisoformat(ts),o,h,l,c,v,
                                         datetime.fromisoformat(ending) if ending else None))
        return {tf: normalize(bars) for tf,bars in series.items() if bars}

    def ingest_stream(self, ticker, mic, events):
        symbol = symbol_name(ticker, mic)
        with self.lock:
            latest = None
            for event in events:
                if (event.kind != "candles" or
                    (event.symbol,event.board) != (ticker,mic) or
                    event.timeframe not in FRAMES):
                    continue
                self._upsert(symbol,event.timeframe,event.candles)
                latest = max(latest or event.received_at, event.received_at)
            if latest:
                self.latest_stream_at[symbol] = latest
                self.analysis_cache.pop(symbol, None)

    def view(self, ticker, mic, timeframe, *, refresh=False, allow_http=False,
             settings=None, progress=None, **ignored):
        symbol = symbol_name(ticker,mic)
        if timeframe not in FRAMES:
            raise ValueError("Неизвестный таймфрейм Finam")
        progress = progress or (lambda message: None)
        settings = settings or IndicatorSettings()
        with self.lock:
            series = self._series(symbol)
            errors = []
            if allow_http and (refresh or not series.get(timeframe) or
                               any(not series.get(tf) for tf in FRAMES)):
                # Fail the selected chart first instead of issuing another six
                # requests for an instrument that cannot be displayed at all.
                for tf in (timeframe, *(tf for tf in FRAMES if tf != timeframe)):
                    if (not refresh and series.get(tf)):
                        continue
                    try:
                        progress("Finam REST: загрузка " + tf)
                        rows = self.api.bars(symbol, tf)
                        if rows:
                            self._upsert(symbol,tf,rows)
                    except Exception as exc:
                        errors.append(f"{tf}: {type(exc).__name__}: {exc}")
                        if tf == timeframe and not series.get(timeframe):
                            raise RuntimeError(f"{symbol} · {timeframe}: {exc}") from None
                series = self._series(symbol)
                if errors and not series.get(timeframe):
                    raise RuntimeError("Finam: не удалось получить историю " +
                                       "; ".join(errors[:3]))
            # Filter auction-only M1/M5 *after* loading the raw source and
            # *before* determining asof/cache keys and any indicator analysis.
            # The raw SQLite archive remains intact; filtering is idempotent.
            series = exclude_stock_closing_auction(series, mic)
            if not series.get(timeframe):
                if not allow_http:
                    raise RuntimeError("Для " + symbol + " нет истории " + timeframe +
                                       ". Разрешите HTTP для первоначальной загрузки.")
                raise RuntimeError("Finam не вернул свечи " + timeframe + " для " + symbol)
            # Finam permissions provide the actual market data delay. Without
            # permission details we conservatively assume 15 minutes.
            delay = self.api.market_delay_minutes(mic)
            observed = min(self.clock(), self.clock()-timedelta(minutes=delay)) if delay else self.clock()
            prior = self.latest_stream_at.get(symbol)
            if prior and delay == 0:
                observed = max(observed, prior)
            cached = self.analysis_cache.get(symbol, {})
            asof = min(observed, candle_close_time(series[timeframe][-1],timeframe))
            valid = (not refresh and cached.get("key")==settings.calculation_key()
                     and cached.get("timeframe")==timeframe
                     and cached.get("asof")==asof
                     and cached.get("size")==tuple((tf,len(b),b[-1]) for tf,b in series.items()))
            if valid:
                main, parents = cached["result"]
            else:
                progress("Finam: расчёт потоков v2.11")
                provenance = {tf:{"mode":"FINAM_NATIVE_CANDLES",
                                  "symbol":symbol,"market":mic,
                                  "native_higher":tf in ("D1","W1","MN1"),
                                  "feed_delay_seconds":delay*60} for tf in series}
                main, parents = analyze_timeframes(series,timeframe,asof=observed,
                                                    settings=settings,provenance=provenance)
                self.analysis_cache[symbol] = dict(
                    key=settings.calculation_key(),timeframe=timeframe,asof=asof,
                    size=tuple((tf,len(b),b[-1]) for tf,b in series.items()),
                    result=(main,parents))
            return main, parents, {
                "symbol":ticker,"board":mic,"tf":timeframe,
                "tier":"stream" if prior else ("network" if allow_http and refresh else "disk"),
                "asof":main.asof.isoformat(),
                "requested_at":self.clock().isoformat(),
                "stream_received_at":prior.isoformat() if prior else None,
                "feed_delay_seconds":delay*60,
                "warning":"Историческая глубина ограничена Finam Trade API; "
                          "полный паритет Pine на других биржах не подтверждён" +
                          (". Не обновлены: " + "; ".join(errors) if errors else ""),
                "stream_repair_required":False
            }
