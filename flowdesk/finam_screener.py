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

from .finam_api import decimal_number, symbol_name, DEPTH_DAYS, FinamAPI
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
    # Per-timeframe explanations for unavailable signals. Never store
    # responses, secrets, error strings or a fabricated direction.
    missing_reasons: tuple[str, ...] = ("",) * len(TIMEFRAMES)

    def reason_map(self):
        return dict(zip(LABELS,self.missing_reasons))

    def as_map(self):
        return dict(zip(LABELS, self.directions))

    def matched(self, selected):
        return any(self.as_map().get(tf) in (-1, 1) for tf in selected)

    def missing(self, selected):
        return any(self.as_map().get(tf) is None for tf in selected)


_ERROR_LABELS={
    "missing_history": "история не загружена",
    "price_or_h1_unavailable": "нет цены или истории H1",
    "price_unavailable": "нет последней цены Finam",
    "h1_unavailable": "H1 недоступен",
    "analysis_unavailable": "расчёт недоступен",
    "unverified": "тикер не подтверждён Finam",
    "rate_limited": "лимит запросов Finam",
    "source_unavailable": "Finam недоступен",
    "scan_incomplete": "проверка не завершена; повтор через 5 мин",
}


def unknown(symbol, code="missing_history", *, reasons=None):
    if reasons is None:
        reasons=(_ERROR_LABELS.get(code,"проверка данных"),)*len(TIMEFRAMES)
    return ScanResult(symbol,(None,)*len(TIMEFRAMES),
                      error_code=code,missing_reasons=tuple(reasons))


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


def select_working_fvg_signals(main, latest_price, *, symbol="", fetch_problems=None):
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
    reasons=[]
    for tf in TIMEFRAMES:
        source=sources.get(tf)
        required=MIN_BARS[tf]
        confirmed=max(0,int(source.confirmed_bars)) if source is not None else 0
        # Confirmed source bars are a lower bound for the working-flow
        # history. A new December futures contract cannot magically supply
        # 30 months of its own prices.
        if not source or not source.candles or confirmed<required:
            direction.append(None)
            error=(fetch_problems or {}).get(tf,"")
            detail=(f"история {confirmed}/{required}" if confirmed
                    else "нет свечей")
            reasons.append(detail+(("; "+error) if error else ""))
            continue
        zone=working_zone(source,cutoff,cutoff)
        direction.append(
            zone.side if zone is not None and
            zone.bottom<=latest_price<=zone.top else 0)
        reasons.append("")
    return ScanResult(symbol,tuple(direction),price=latest_price,
                      missing_reasons=tuple(reasons))



def _backfill_higher_history(service, symbol, now, series, *,
                             cancelled=lambda: False, time_sleep=time.sleep):
    """Resume bounded Finam backfill for short D/W/M source histories.

    At most three extra requests per instrument and pass. All successful
    candles go into the existing Finam-only SQLite cache; progress is kept
    in memory so later five-minute passes resume from the prior boundary.
    A 429 is propagated to stop the entire scan, not disguised as no bars.
    An old individual future is never stitched to another contract.
    """
    if not isinstance(getattr(service, "api", None), FinamAPI):
        return {}
    state=getattr(service,"scanner_history_progress",None)
    if state is None:
        state={}
        service.scanner_history_progress=state
    problems={}
    budget=5
    for tf in ("W1","MN1","D1"):
        if cancelled():
            raise InterruptedError()
        bars=series.get(tf) or ()
        cutoff=now-timedelta(days=DEPTH_DAYS[tf])
        confirmed=sum(candle_close_time(bar,tf)<=now for bar in bars)
        if confirmed>=MIN_BARS[tf]:
            continue
        key=(symbol,tf)
        progress=state.get(key,{})
        if progress.get("exhausted"):
            problems[tf]="доступный период Finam проверен"
            continue
        cursor=progress.get("cursor")
        if cursor is None:
            cursor=min((bar.time for bar in bars),default=now)
        chunk=timedelta(days=90 if tf=="D1" else 365)
        frame_budget=2 if tf in ("W1","MN1") else 1
        while budget and frame_budget and cursor>cutoff and confirmed<MIN_BARS[tf]:
            if cancelled():
                raise InterruptedError()
            start=max(cutoff,cursor-chunk)
            try:
                older=service.api.bars(symbol,tf,start=start,end=cursor)
            except Exception as exc:
                if getattr(exc,"http_status",None)==429:
                    raise
                code=getattr(exc,"http_status",None)
                problems[tf]=(
                    "нет доступа Finam (403)" if code==403 else
                    "ошибка авторизации Finam (401)" if code==401 else
                    "контракт не найден Finam (404)" if code==404 else
                    "дозагрузка Finam недоступна")
                break
            if older:
                with service.lock:
                    service._upsert(symbol,tf,older)
                # Compare timestamps, not response count: intervals may
                # overlap an already cached candle at a boundary.
                known={bar.time for bar in bars}
                bars=[*bars,*(bar for bar in older if bar.time not in known)]
                confirmed=sum(candle_close_time(bar,tf)<=now for bar in bars)
            cursor=start
            budget-=1
            frame_budget-=1
            state[key]={"cursor":cursor,"exhausted":cursor<=cutoff}
            if budget:
                time_sleep(.4)
        if tf not in problems and confirmed<MIN_BARS[tf]:
            problems[tf]=(
                "доступный период Finam проверен" if cursor<=cutoff
                else "дозагрузка истории")
        if budget==0:
            break
    return problems

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
    fetch_problems={}
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
                fetch_problems[tf]="Finam не вернул свечи"
        except Exception as exc:
            code=getattr(exc,"http_status",None)
            if code==429:
                raise  # Worker will back off for the entire remaining batch.
            errors.append("fetch_"+tf)
            fetch_problems[tf]=(
                "нет доступа Finam (403)" if code==403 else
                "контракт не найден Finam (404)" if code==404 else
                "ошибка авторизации Finam (401)" if code==401 else
                "загрузка Finam недоступна")
        if cancelled():
            raise InterruptedError()
        # Avoid hitting Finam's request limit with a synchronous 38x6 burst.
        time_sleep(.4)
    series=service._series(symbol)
    if not series.get("H1"):
        return unknown(symbol,"h1_unavailable",
                       reasons=tuple(fetch_problems.get(tf,"H1 недоступен")
                                     for tf in TIMEFRAMES))
    fetch_problems.update(_backfill_higher_history(
        service,symbol,now,series,cancelled=cancelled,
        time_sleep=time_sleep))
    series=exclude_stock_closing_auction(service._series(symbol),mic)
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
    result=select_working_fvg_signals(
        main,price,symbol=symbol,fetch_problems=fetch_problems)
    return ScanResult(symbol,result.directions,price,origin,
                      "partial_source" if errors else "",
                      result.missing_reasons)


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
        for index,entry in enumerate(self.instruments):
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
                    # An early stop must not make the remaining instruments
                    # look like they have insufficient weekly/monthly bars.
                    # Their exact state is rate-limited, not history-short.
                    for pending in self.instruments[index+1:]:
                        if self.isInterruptionRequested():
                            break
                        self.row.emit(unknown(pending.symbol,"rate_limited"))
                    break
                continue
            self.row.emit(result)
        self.finished_pass.emit()
