"""Read-only H4/D/W/M FVG screener for the selected terminal universe.

Uses the pinned v2.11 FlowEngine with the user's v2.13 summary predicate,
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

from .finam_api import decimal_number, DEPTH_DAYS, FinamAPI, parse_instant
from .market_symbols import symbol_name,BINANCE
from .finam_auction import exclude_stock_closing_auction
from .fvg_summary import summary_fvg_side
from .publication import chart_cutoff, published_state
from .finam_market import TTL_SECONDS
from .provider_candles import native_higher_enabled
from .asyncio_runtime import prepare_asyncio_runtime

from .terminal_policy import FLOW_TIMEFRAMES, SUMMARY_LABELS

TIMEFRAMES = FLOW_TIMEFRAMES
LABELS = SUMMARY_LABELS
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

    def flow_unformed(self, timeframe):
        # Keep source/access failures visible separately. These two explicit
        # analytical states are omitted from the filtered watchlist only.
        reason=self.reason_map().get(timeframe,"").split(";",1)[0]
        return self.as_map().get(timeframe) is None and reason in (
            "поток ещё не сформирован","нет подтверждённого состояния")


_ERROR_LABELS={
    "missing_history": "история не загружена",
    "price_or_history_unavailable": "нет цены или истории",
    "price_unavailable": "нет последней цены Finam",
    "history_unavailable": "история недоступна",
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
    if symbol.endswith("@"+BINANCE):
        reasons=tuple(reason.replace("Finam","Binance") for reason in reasons)
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
    """v2.13 summary rule for each independently confirmed source flow.

    Never fall back to an older FVG if the working guard is broken. Every TF
    has its own most recent closed source state; price is the same latest
    Finam trade/available last candle across all four TFs. A developing,
    unconfirmed FVG does not produce a signal.
    """
    if not main or not getattr(main, "candles", None) or latest_price is None:
        return unknown(symbol, "price_or_history_unavailable")
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
        direction.append(summary_fvg_side(source,cutoff,latest_price,
                         live=tf==main.timeframe))
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
    if getattr(service.api,'session_candles',False) and not native_higher_enabled(service.api):
        from .session_store import backfill
        return backfill(service,symbol,now,cancelled=cancelled)
    if not isinstance(getattr(service, "api", None), FinamAPI):
        return {}
    if native_higher_enabled(service.api):
        from . import moex_trading_family as moex
        if moex.enabled(symbol) and not moex.archive_ready(service._series(symbol),symbol):
            return moex.backfill(service,symbol,now,cancelled=cancelled)
    state=getattr(service,"scanner_history_progress",None)
    if state is None:
        with service.lock:
            state=getattr(service,"scanner_history_progress",None)
            if state is None:
                load=getattr(service,"load_history_progress",None)
                state=load() if callable(load) else {}
                service.scanner_history_progress=state
    problems={}
    budget=4 if native_higher_enabled(service.api) else 5
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
    if native_higher_enabled(service.api):
        from .session_store import backfill
        problems.update(backfill(service,symbol,now,cancelled=cancelled))
    return problems

_MOEX_PREPARATION_MESSAGES={
    "incomplete_daily_coverage":"дневная история Finam ещё подготавливается",
    "lower_history_unavailable":"нет исходных дневных свечей Finam",
    "native_snapshot_unconfirmed":"Finam ещё не подтвердил исходную дневную свечу",
    "opening_source_unavailable":"не подтверждена цена открытия непрерывных торгов",
    "opening_source_conflict":"конфликт исходной цены открытия Finam",
    "calendar_history_unknown":"не подтверждён календарь для исходной истории",
    "calendar_source_conflict":"даты исходной истории противоречат календарю MOEX",
    "instrument_calendar_unknown":"не подтверждён календарь инструмента",
}


def _bootstrap_moex_for_scan(service,symbol,raw,observed,*,cancelled):
    """Use the same bounded, resumable D/H1 bootstrap as the selected chart.

    The existing per-symbol MOEX lock serializes scanner/chart preparation.
    No backfill is needed once the verified D/W/M family is available.
    """
    if not native_higher_enabled(service.api):
        return raw,{}
    from . import moex_trading_family as moex
    if not moex.enabled(symbol):
        return raw,{}
    if moex.archive_ready(raw,symbol) and moex.opening_ready(raw,symbol,observed):
        return raw,{}
    # An empty/denied provider history must not trigger four redundant
    # archive requests. The initial refresh supplies native D/W/M first.
    if not any(raw.get(tf) for tf in ("D1","W1","MN1")):
        return raw,{"D1":"нет исходных дневных свечей Finam"}
    if cancelled():
        raise InterruptedError()
    try:
        moex.backfill(service,symbol,service.clock(),cancelled=cancelled)
    except InterruptedError:
        raise
    except Exception as exc:
        code=getattr(exc,"http_status",None)
        if code==429:
            raise
        message={401:"ошибка авторизации Finam (401)",
                 403:"нет доступа к истории Finam (403)",
                 404:"контракт не найден Finam (404)"}.get(code,
                 "подготовка истории Finam недоступна")
        return service._series(symbol),{"D1":message,"W1":message,"MN1":message}
    return service._series(symbol),{}


def scan_one(service, ticker, mic, settings, *, cancelled=lambda:False,
             time_sleep=time.sleep):
    scope=getattr(service,'http_scan',None)
    with scope() if callable(scope) else nullcontext():
        return _scan_one(service,ticker,mic,settings,cancelled=cancelled,time_sleep=time_sleep)


def _scan_one(service, ticker, mic, settings, *, cancelled=lambda:False,
              time_sleep=time.sleep):
    """Read only Finam REST and the same local SQLite store as the terminal.

    Historical candles are fetched only at each TF's refresh deadline, never
    from MOEX ISS. Long-running network requests run in a QThread. The
    currently selected chart never changes during a scan.
    """
    symbol=symbol_name(ticker,mic)
    if cancelled():
        raise InterruptedError()
    getter=getattr(service.api,'asset_tick_size',None)
    if callable(getter):
        try:getter(symbol)
        except InterruptedError:raise
        except Exception:pass
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
        if price is not None:origin=getattr(service.api,"provider_name","Finam").lower()+"_last"
    except InterruptedError:raise
    except Exception as exc:
        if getattr(exc,"http_status",None)==429:raise
    fetch_problems=service.refresh_series(symbol,TIMEFRAMES,priority=10,cancelled=cancelled)
    if price is None:
        fetch_problems.update(service.refresh_series(symbol,("M5",),priority=10,cancelled=cancelled))
    series=service._series(symbol)
    delay=service.api.market_delay_minutes(mic)
    observed=service.clock()-timedelta(minutes=delay or 0)
    repair=getattr(service,'repair_source_evidence',None)
    if ('H1' not in fetch_problems and callable(repair) and
            repair(symbol,series,observed,cancelled=cancelled,priority=10)):
        series=service._series(symbol)
        observed=service.clock()-timedelta(minutes=service.api.market_delay_minutes(mic) or 0)
    # First scan of a MOEX future must prepare its D/W/M source family
    # without requiring the user to open the D chart. This operation is
    # bounded, cancelable and resumes from durable Finam archive cursors.
    series,bootstrap_problems=_bootstrap_moex_for_scan(
        service,symbol,series,observed,cancelled=cancelled)
    for tf,reason in bootstrap_problems.items():
        if tf in TIMEFRAMES:
            fetch_problems.setdefault(tf,reason)
    prepare=getattr(service,'prepare_series',None)
    if callable(prepare):series=prepare(symbol,series,observed)
    else:series=exclude_stock_closing_auction(series,mic)
    metadata=getattr(series,'session_metadata',{}) or {}
    # A complete MOEX archive can still contain an old D1 that was observed
    # before the session closed. A three-candle tail cannot reconfirm it.
    # Recheck one exact raw source before deciding that D/W/M are unavailable.
    from . import moex_trading_family as moex
    if (metadata.get('version')==moex.POLICY and
            any(issue=='native_snapshot_unconfirmed'
                for tf,issue in (metadata.get('issues',{}) or {}).items()
                if tf in ('D1','W1','MN1'))):
        if moex.recheck_unconfirmed_snapshot(
                service,symbol,service._series(symbol),observed,
                cancelled=cancelled,priority=10):
            series=prepare(symbol,service._series(symbol),observed)
            metadata=getattr(series,'session_metadata',{}) or {}
    # Native equities and other markets may have control diagnostics despite
    # valid higher candles; they are not MOEX futures bootstrap failures.
    from .moex_trading_family import POLICY as MOEX_POLICY
    if metadata.get('version')==MOEX_POLICY:
        for tf,issue in (metadata.get('issues',{}) or {}).items():
            # Never replace source-specific HTTP permission/transport failures.
            if tf in TIMEFRAMES and not series.get(tf) and issue:
                fetch_problems.setdefault(tf,
                    _MOEX_PREPARATION_MESSAGES.get(issue,"не подтверждена исходная история Finam"))
    analysis_tf=next((tf for tf in TIMEFRAMES if series.get(tf)),None)
    if analysis_tf is None:
        return unknown(symbol,'history_unavailable',
                       reasons=tuple(fetch_problems.get(tf,'история недоступна') for tf in TIMEFRAMES))
    if cancelled():
        raise InterruptedError()
    try:
        main, _ = service.analyze_cached(symbol,analysis_tf,series,settings,observed,terminal=True)
    except (ValueError,IndexError,TypeError,AttributeError):
        return unknown(symbol,"analysis_unavailable")
    if price is None and not quote_problem:
        # Finam-only fallback when the quote endpoint is unavailable. Use
        # the last known M5 close, then H4 close; never invent a bid or trade.
        recent=series.get("M5") or series.get("H4") or ()
        available=[c for c in recent if c.time<=observed]
        if available:
            price=_price(available[-1].close)
            origin=getattr(service.api,"provider_name","Finam").lower()+"_candle_close"
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
                 evaluate=scan_one,max_workers=3,backfill=False):
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
            service=(self.service.for_market(entry.symbol.rsplit("@",1)[1])
                     if callable(getattr(self.service,"for_market",None)) else self.service)
            scope=getattr(getattr(service,"api",None),"request_scope",None)
            with scope(priority=20 if extend else 10,cancelled=cancelled) if scope else nullcontext():
                if extend:
                    symbol=symbol_name(*entry.symbol.split("@",1))
                    _backfill_higher_history(service,symbol,service.clock(),
                        service._series(symbol),cancelled=cancelled)
                return self.evaluate(service,*entry.symbol.split("@",1),self.settings,cancelled=cancelled)
        self.progress.emit(0,len(self.instruments))
        pool=concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers,thread_name_prefix="FinamScan")
        try:
            # First publish all instruments; archive backfill has a separate low-priority pass.
            for extend in (False,True):
                if extend and (not self.backfill or not isinstance(getattr(self.service,"api",None),FinamAPI)):
                    break
                pending={}
                cursor=iter((index,entry) for index,entry in enumerate(self.instruments)
                            if not extend or not entry.symbol.endswith("@"+BINANCE))
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
