"""Read-only embedded Finam FVG screener for the *existing* 38 instruments.

Uses the same pinned v2.11 FlowEngine / selected working guard as CandleChart,
not a separate MOEX feed or reimplementation of flow reversal rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
import time

from PySide6.QtCore import QThread, Signal

from .finam_api import decimal_number, symbol_name
from .finam_auction import exclude_stock_closing_auction
from .fvg_display import working_zone
from .market_data import candle_close_time
from .pipeline import analyze_timeframes
from .publication import chart_cutoff

TIMEFRAMES = ("H1", "H4", "D1", "W1", "MN1")
LABELS = ("H1", "H4", "D", "W", "M")
DISPLAY = dict(zip(TIMEFRAMES, LABELS))
INTERNAL = dict(zip(LABELS, TIMEFRAMES))
# Conservative minimum historical bar counts. Missing source is unknown,
# never a fabricated 0/SHORT signal.
MIN_BARS = {"H1": 70, "H4": 50, "D1": 45, "W1": 35, "MN1": 30}
TTL_SECONDS = {"H1": 300, "H4": 900, "D1": 1800,
               "W1": 3600, "MN1": 14400, "M1": 300}


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    directions: tuple[int | None, ...]  # +1 LONG, -1 SHORT, 0 no entry, None unknown
    price: float | None = None
    price_origin: str = ""
    error_code: str = ""

    def as_map(self):
        return dict(zip(LABELS, self.directions))

    def matched(self, selected):
        return any(self.as_map().get(tf) in (-1, 1) for tf in selected)

    def missing(self, selected):
        return any(self.as_map().get(tf) is None for tf in selected)


def unknown(symbol, code="missing_history"):
    return ScanResult(symbol, (None,) * len(TIMEFRAMES), error_code=code)


def _price(value):
    try:
        parsed=decimal_number(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def quote_last(response, symbol=None):
    """Return LAST only from the exact requested symbol, never a neighbour."""
    if not isinstance(response, dict):
        return None
    quotes=response.get("quote")
    if isinstance(quotes, list):
        response=next((item for item in quotes
                       if isinstance(item,dict) and
                       (not symbol or not item.get("symbol") or
                        item["symbol"].upper()==symbol.upper())), {})
    elif isinstance(quotes, dict):
        response=quotes
    if symbol and response.get("symbol") and (
            str(response["symbol"]).upper()!=symbol.upper()):
        return None
    price=response.get("last")
    return _price(price)


def select_working_fvg_signals(main, latest_price, *, symbol=""):
    """One active *selected* FVG for each independently confirmed flow.

    Never fall back to an older FVG if the working guard is broken. Every TF
    has its own most recent closed source state; price is the same latest
    Finam trade/available last candle across all five TFs. A developing,
    unconfirmed FVG does not produce a signal.
    """
    if not main or not getattr(main, "candles", None) or latest_price is None:
        return unknown(symbol, "price_or_h1_unavailable")
    sources={**(getattr(main,"summary_sources",{}) or {}),
             main.timeframe:main}
    cutoff=chart_cutoff(main)
    direction=[]
    for tf in TIMEFRAMES:
        source=sources.get(tf)
        if (not source or len(source.candles)<MIN_BARS[tf] or
                source.confirmed_bars < MIN_BARS[tf]):
            direction.append(None)
            continue
        zone=working_zone(source,cutoff,cutoff)
        direction.append(
            zone.side if zone is not None and
            zone.bottom<=latest_price<=zone.top else 0)
    return ScanResult(symbol,tuple(direction),price=latest_price)


def scan_one(service, ticker, mic, settings, *, cancelled=lambda:False,
             time_sleep=time.sleep):
    """Read only Finam REST and the same local SQLite store as the terminal.

    Historical candles are fetched only at each TF's refresh deadline, never
    from MOEX ISS. Long-running network requests run in a QThread. The
    currently selected chart never changes during a scan.
    """
    symbol=symbol_name(ticker,mic)
    if cancelled():
        raise InterruptedError()
    now=service.clock()
    series=service._series(symbol)
    last_pull=getattr(service,"scanner_last_pull",None)
    if last_pull is None:
        service.scanner_last_pull={}
        last_pull=service.scanner_last_pull
    errors=[]
    for tf in (*TIMEFRAMES,"M1"):
        if cancelled():
            raise InterruptedError()
        pulled=last_pull.get((symbol,tf))
        fresh=bool(pulled and (now-pulled).total_seconds()<TTL_SECONDS[tf])
        if fresh and series.get(tf):
            continue
        try:
            bars=service.api.bars(symbol,tf)
            if bars:
                with service.lock:
                    service._upsert(symbol,tf,bars)
                last_pull[(symbol,tf)]=now
            elif not series.get(tf):
                errors.append("missing_"+tf)
        except Exception as exc:
            if getattr(exc,"http_status",None)==429:
                raise  # Worker will back off for the entire remaining batch.
            errors.append("fetch_"+tf)
        if cancelled():
            raise InterruptedError()
        # Avoid hitting Finam's request limit with a synchronous 38x6 burst.
        time_sleep(.12)
    series=exclude_stock_closing_auction(service._series(symbol),mic)
    if not series.get("H1"):
        return unknown(symbol,"h1_unavailable")
    delay=service.api.market_delay_minutes(mic)
    observed=now-timedelta(minutes=delay or 0)
    if cancelled():
        raise InterruptedError()
    try:
        main, _ = analyze_timeframes(series,"H1",asof=observed,
                                      settings=settings)
    except (ValueError,IndexError,TypeError,AttributeError):
        return unknown(symbol,"analysis_unavailable")
    price=None
    origin=""
    try:
        price=quote_last(service.api.latest_quote(symbol),symbol)
        if price is not None:
            origin="finam_last"
    except Exception:
        pass
    if price is None:
        # Finam-only fallback when the quote endpoint is unavailable. Use
        # the last known M1 close, then H1 close; never invent a bid or trade.
        recent=series.get("M1") or series["H1"]
        available=[c for c in recent if c.time<=observed]
        if available:
            price=_price(available[-1].close)
            origin="finam_candle_close"
    if price is None:
        return unknown(symbol,"price_unavailable")
    result=select_working_fvg_signals(main,price,symbol=symbol)
    return ScanResult(symbol,result.directions,price,origin,
                      "partial_source" if errors else "")


class FinamScreenerWorker(QThread):
    row=Signal(object)
    finished_pass=Signal()

    def __init__(self, service, instruments, settings, parent=None, *,
                 evaluate=scan_one):
        super().__init__(parent)
        self.service=service
        self.instruments=tuple(instruments)
        self.settings=settings
        self.evaluate=evaluate

    def run(self):
        for entry in self.instruments:
            if self.isInterruptionRequested():
                break
            if not entry.verified or "@" not in entry.symbol:
                self.row.emit(unknown(entry.symbol,"unverified"))
                continue
            ticker,mic=entry.symbol.split("@",1)
            try:
                result=self.evaluate(self.service,ticker,mic,self.settings,
                                     cancelled=self.isInterruptionRequested)
            except InterruptedError:
                break
            except Exception as exc:
                status=getattr(exc,"http_status",None)
                # No exception message, URL, token or account ID can reach UI.
                result=unknown(entry.symbol,
                               "rate_limited" if status==429 else "source_unavailable")
                self.row.emit(result)
                if status==429:
                    break
                continue
            self.row.emit(result)
        self.finished_pass.emit()
