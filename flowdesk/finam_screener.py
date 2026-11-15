"""Read-only embedded Finam FVG screener for the *existing* 38 instruments.

Uses the same pinned v2.11 FlowEngine / selected working guard as CandleChart,
not a separate MOEX feed or reimplementation of flow reversal rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
import time
import threading
import concurrent.futures
from contextlib import nullcontext

from PySide6.QtCore import QThread, Signal

from .finam_api import decimal_number, symbol_name, DEPTH_DAYS, FinamAPI, parse_instant
from .finam_auction import exclude_stock_closing_auction
from .fvg_display import working_zone
from .publication import chart_cutoff, published_state
from .finam_market import TTL_SECONDS
from .asyncio_runtime import prepare_asyncio_runtime

TIMEFRAMES = ("H1", "H4", "D1", "W1", "MN1")
LABELS = ("H1", "H4", "D", "W", "M")
DISPLAY = dict(zip(TIMEFRAMES, LABELS))
INTERNAL = dict(zip(LABELS, TIMEFRAMES))
# Signal readiness comes from the confirmed flow, not a fixed bar count.


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
    "stale_quote": "котировка Finam устарела",
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


def _quote_record(response, symbol=None):
    """Choose one record for both LAST and its timestamp validation."""
    if not isinstance(response, dict):
        return {}
    quotes=response.get("quote")
    if isinstance(quotes, list):
        if symbol:
            # Never accept an unlabelled quote before the exact requested
            # symbol: it can belong to another item in a batch.
            response=next((item for item in quotes
                           if isinstance(item,dict) and
                           str(item.get("symbol","")).upper()==symbol.upper()), {})
            # Single-instrument REST responses may omit the symbol. Accept
            # only if there is exactly one item; never guess among many.
            if not response and len(quotes)==1 and isinstance(quotes[0],dict) and not quotes[0].get("symbol"):
                response=quotes[0]
        else:
            response=quotes[0] if len(quotes)==1 and isinstance(quotes[0],dict) else {}
    elif isinstance(quotes, dict):
        response=quotes
    if symbol and response.get("symbol") and (
            str(response["symbol"]).upper()!=symbol.upper()):
        return {}
    return response


def quote_last(response, symbol=None):
    """Return LAST only from the exact requested symbol, never a neighbour."""
    return _price(_quote_record(response,symbol).get("last"))


# Analytical signals may use the last exchange trade across weekends,
# holidays and market closures. Reject only a trade older than seven days.
MAX_LAST_AGE_SECONDS = 7 * 24 * 60 * 60


def quote_age_problem(response, observed, symbol, *,
                      max_seconds=MAX_LAST_AGE_SECONDS):
    """Reject timestamped LAST older than seven calendar days.

    This deliberately allows last-traded prices across non-trading days;
    it is an analytical lookback limit, not a claim of a live quote.
    Missing timestamps retain the legacy fallback policy.
    """
    response=_quote_record(response,symbol)
    time_key=next((key for key in ("timestamp","trade_time","last_time")
                   if key in response),None)
    if time_key is None:
        return ""
    stamp=response[time_key]
    try:
        trade_time=parse_instant(stamp)
    except (TypeError,ValueError,KeyError,OverflowError):
        return "время последней сделки Finam не распознано"
    delta=(observed-trade_time).total_seconds()
    if delta > max_seconds:
        return "последняя сделка устарела"
    if delta < -60:
        return "время сделки опережает наблюдаемый рынок"
    return ""


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
        detail=""
        if not source or not source.candles:
            detail="нет свечей"
        else:
            state,_,_=published_state(source,cutoff)
            if state is None:
                detail="нет подтверждённого состояния"
            elif state.side not in (-1,1) or state.lo is None or state.hi is None:
                detail="поток ещё не сформирован"
        if detail:
            direction.append(None)
            error=(fetch_problems or {}).get(tf,"")
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

    At most five extra requests per instrument and pass. All successful
    candles go into the existing Finam-only SQLite cache; progress is kept
    in memory so later five-minute passes resume from the prior boundary.
    A 429 is propagated to stop the entire scan, not disguised as no bars.
    Continue to the existing depth boundary independently of signal readiness.
    An old individual future is never stitched to another contract.
    """
    if not isinstance(getattr(service, "api", None), FinamAPI):
        return {}
    state=getattr(service,"scanner_history_progress",None)
    if state is None:
        with service.lock:
            state=getattr(service,"scanner_history_progress",None)
            if state is None:
                load=getattr(service,"load_history_progress",None)
                state=load() if callable(load) else {}
                service.scanner_history_progress=state
    problems={}
    budget=5
    for tf in ("W1","MN1","D1"):
        if cancelled():
            raise InterruptedError()
        bars=series.get(tf) or ()
        # The five-year API limit is PER REQUEST for MN1, not the maximum
        # lifetime of the instrument. Live Finam returned SBER from 1999.
        # Keep daily/weekly policies unchanged; extend only monthly stocks.
        cutoff=(datetime(1990,1,1,tzinfo=timezone.utc)
                if tf=="MN1" and symbol.upper().endswith("@MISX")
                else now-timedelta(days=DEPTH_DAYS[tf]))
        key=(symbol,tf)
        progress=state.get(key,{})
        cursor=progress.get("cursor")
        if cursor is None:
            cursor=min((bar.time for bar in bars),default=now)
        # Old monthly records were marked exhausted at the former five-year
        # boundary. Migrate only those records, never repeatedly reopen a
        # genuinely empty older window on every five-minute scan.
        legacy_cutoff=now-timedelta(days=DEPTH_DAYS[tf])
        legacy_monthly=(tf=="MN1" and symbol.upper().endswith("@MISX")
                        and abs((cursor-legacy_cutoff).total_seconds())<35*86400)
        if progress.get("exhausted") and not legacy_monthly:
            problems[tf]="архивное окно проверено; более ранние данные не гарантированы"
            continue
        if legacy_monthly:
            progress={"cursor":cursor,"exhausted":False}
        chunk=timedelta(days=90 if tf=="D1" else
                        (5*365 if tf=="MN1" else 365))
        frame_budget=2 if tf in ("W1","MN1") else 1
        while budget and frame_budget and cursor>cutoff:
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
            cursor=start
            budget-=1
            frame_budget-=1
            # A completely empty old window is a stopping heuristic, not
            # evidence that ALL dates before it are unavailable.
            stop_on_empty=not older
            state[key]={"cursor":cursor,"exhausted":cursor<=cutoff or stop_on_empty}
            save=getattr(service,"save_history_progress",None)
            if callable(save):save(symbol,tf,state[key])
            if stop_on_empty:
                problems[tf]="старое окно Finam пусто; более ранняя история не проверена"
                break
        if tf not in problems:
            problems[tf]=(
                "достигнута граница проверенных окон" if cursor<=cutoff
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
    price=None
    origin=""
    quote_problem=""
    try:
        last_quote=service.latest_quote(symbol,cancelled=cancelled)
        observed_quote=now-timedelta(minutes=service.api.market_delay_minutes(mic))
        quote_problem=quote_age_problem(last_quote,observed_quote,symbol)
        if not quote_problem:
            price=quote_last(last_quote,symbol)
        if price is not None:origin="finam_last"
    except InterruptedError:raise
    except Exception as exc:
        if getattr(exc,"http_status",None)==429:raise
    fetch_problems=service.refresh_series(symbol,TIMEFRAMES,priority=10,cancelled=cancelled)
    if price is None:
        fetch_problems.update(service.refresh_series(symbol,("M1",),priority=10,cancelled=cancelled))
    series=service._series(symbol)
    if not series.get("H1"):
        return unknown(symbol,"h1_unavailable",
                       reasons=tuple(fetch_problems.get(tf,"H1 недоступен")
                                     for tf in TIMEFRAMES))
    series=exclude_stock_closing_auction(service._series(symbol),mic)
    delay=service.api.market_delay_minutes(mic)
    observed=service.clock()-timedelta(minutes=delay or 0)
    if cancelled():
        raise InterruptedError()
    try:
        main, _ = service.analyze_cached(symbol,"H1",series,settings,observed)
    except (ValueError,IndexError,TypeError,AttributeError):
        return unknown(symbol,"analysis_unavailable")
    if price is None and not quote_problem:
        # Finam-only fallback when the quote endpoint is unavailable. Use
        # the last known M1 close, then H1 close; never invent a bid or trade.
        recent=series.get("M1") or series["H1"]
        available=[c for c in recent if c.time<=observed]
        if available:
            price=_price(available[-1].close)
            origin="finam_candle_close"
    if quote_problem:
        return unknown(symbol,"stale_quote",reasons=(quote_problem,)*len(TIMEFRAMES))
    if price is None:
        return unknown(symbol,"price_unavailable")
    result=select_working_fvg_signals(
        main,price,symbol=symbol,fetch_problems=fetch_problems)
    return ScanResult(symbol,result.directions,price,origin,
                      "partial_source" if fetch_problems else "",
                      result.missing_reasons)


class FinamScreenerWorker(QThread):
    row=Signal(object)
    progress=Signal(int,int)
    finished_pass=Signal()

    def __init__(self, service, instruments, settings, parent=None, *,
                 evaluate=scan_one,max_workers=3,backfill=True):
        super().__init__(parent)
        self.service=service
        self.instruments=tuple(instruments)
        self.settings=settings
        self.evaluate=evaluate
        self.max_workers=max_workers
        self.backfill=backfill

    def run(self):
        prepare_asyncio_runtime()
        stop=threading.Event()
        cancelled=lambda:self.isInterruptionRequested() or stop.is_set()
        completed=set()
        outcomes={}
        rate_limited=False
        def one(entry,extend=False):
            if cancelled():raise InterruptedError()
            if not entry.verified or "@" not in entry.symbol:return unknown(entry.symbol,"unverified")
            scope=getattr(getattr(self.service,"api",None),"request_scope",None)
            with scope(priority=20 if extend else 10,cancelled=cancelled) if scope else nullcontext():
                if extend:
                    symbol=symbol_name(*entry.symbol.split("@",1))
                    _backfill_higher_history(self.service,symbol,self.service.clock(),
                        self.service._series(symbol),cancelled=cancelled)
                return self.evaluate(self.service,*entry.symbol.split("@",1),self.settings,cancelled=cancelled)
        self.progress.emit(0,len(self.instruments))
        pool=concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers,thread_name_prefix="FinamScan")
        try:
            # First publish all instruments; archive backfill has a separate low-priority pass.
            for extend in (False,True):
                if extend and (not self.backfill or not isinstance(getattr(self.service,"api",None),FinamAPI)):
                    break
                pending={}
                cursor=iter(enumerate(self.instruments))
                exhausted=False
                while not cancelled():
                    while len(pending)<self.max_workers and not exhausted:
                        try:index,entry=next(cursor)
                        except StopIteration:
                            exhausted=True
                            break
                        pending[pool.submit(one,entry,extend)]=(index,entry)
                    if not pending:break
                    ready,_=concurrent.futures.wait(pending,timeout=.1,
                        return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in ready:
                        index,entry=pending.pop(future)
                        try:result=future.result()
                        except InterruptedError:continue
                        except Exception as exc:
                            limited=getattr(exc,"http_status",None)==429
                            # Archive failures must not erase already valid
                            # primary-pass signals. Keep their earlier result.
                            if extend and index in outcomes:
                                if limited:
                                    rate_limited=True
                                    stop.set()
                                continue
                            result=unknown(entry.symbol,"rate_limited" if limited else "source_unavailable")
                            if limited:
                                rate_limited=True
                                stop.set()
                        if not extend or outcomes.get(index)!=result:
                            self.row.emit(result)
                        outcomes[index]=result
                        if not extend:
                            completed.add(index)
                            self.progress.emit(len(completed),len(self.instruments))
                if cancelled():break
        finally:
            stop.set()
            pool.shutdown(wait=True,cancel_futures=True)
            if rate_limited and not self.isInterruptionRequested():
                for index,entry in enumerate(self.instruments):
                    if index not in completed:
                        self.row.emit(unknown(entry.symbol,"rate_limited"))
                        completed.add(index)
                        self.progress.emit(len(completed),len(self.instruments))
            self.finished_pass.emit()
