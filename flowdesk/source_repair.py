"""Revalidate legacy lower bars whose delivery/finalization evidence is absent.

Raw existence is not proof of a finalized snapshot. Repair a recent interior
evidence gap with one bounded same-provider HTTP request, rather than dropping
its prices indefinitely while the full archive walks back from today.
"""
from contextlib import nullcontext
from datetime import datetime,timedelta

from .market_data import candle_close_time
from .session_store import source_end,record_response
from .finam_auction import is_stock_closing_auction_bar

RETRY_SECONDS=300
CHECK_SECONDS=60
WINDOW_DAYS=30


def repair_source_evidence(service,symbol,raw,observed,*,cancelled=lambda:False,priority=0):
    state=getattr(raw,'source_state',None)
    if not state or not getattr(service.api,'session_candles',False):return False
    if not callable(getattr(service.api,'bars',None)):return False
    if cancelled():raise InterruptedError()
    now=service.clock()
    with service.lock:
        checks=service._source_repair_checked
        prior=checks.get(symbol)
        if prior is not None and 0<=(now-prior).total_seconds()<CHECK_SECONDS:return False
        checks[symbol]=now
    # On shares native H1 mixes the closing auction: canonical hours may
    # require M5 instead. Futures/crypto can repair their complete native H1.
    stock=symbol.rsplit('@',1)[1] in ('MISX','XMOE')
    frames=('M5','H1') if stock else ('H1',)
    observations={(tf,datetime.fromisoformat(ts)):datetime.fromisoformat(stamp)
                  for tf,ts,stamp in state.get('observations',()) if tf in frames}
    for tf in frames:
        rows=raw.get(tf,())
        # Legacy holes are the migration defect; prioritize them ahead of a
        # recently expired live snapshot (normally covered by the tail refresh).
        legacy=any(observations.get((tf,b.time)) is None and
                   candle_close_time(b,tf)<=observed and
                   not is_stock_closing_auction_bar(b,tf,symbol.rsplit('@',1)[1]) for b in rows)
        start=end=None
        for bar in reversed(rows):
            nominal=candle_close_time(bar,tf)
            known=observations.get((tf,bar.time))
            if legacy and known is not None:
                if start is not None:break
                continue
            if known is not None and known>=nominal:
                if start is not None:break
                continue
            if is_stock_closing_auction_bar(bar,tf,symbol.rsplit('@',1)[1]):continue
            ending=source_end(service,symbol,tf,bar)
            if ending>observed:continue  # Never seal a developing candle.
            if known is not None and known>=ending:
                if start is not None:break
                continue
            if end is None:end=ending
            if bar.time<end-timedelta(days=WINDOW_DAYS):break
            start=bar.time
        if start is None:continue
        lock=service._key_lock(service._pull_locks,(symbol,'SOURCE_EVIDENCE_REPAIR'))
        service._acquire(lock,cancelled)
        try:
            attempts=getattr(service,'_source_repair_attempts',None)
            if attempts is None:
                attempts={};service._source_repair_attempts=attempts
            key=(symbol,tf)
            now=service.clock()
            prior=attempts.get(key)
            if prior is not None and 0<=(now-prior).total_seconds()<RETRY_SECONDS:
                continue
            if cancelled():raise InterruptedError()
            generation=service._generation
            attempts[key]=now
            scope=getattr(service.api,'request_scope',None)
            try:
                with scope(priority=priority,cancelled=cancelled) if scope else nullcontext():
                    bars=service.api.bars(symbol,tf,start=start,end=end)
                if cancelled() or generation!=service._generation:raise InterruptedError()
                if any(not start<=b.time<end for b in bars):
                    raise ValueError('Source repair outside requested interval')
                if not bars:return False
                with service.lock:
                    if generation!=service._generation:raise InterruptedError()
                    service._upsert(symbol,tf,bars)
                record_response(service,symbol,tf,bars,start,end,observed)
                return True
            except InterruptedError:
                attempts.pop(key,None);checks.pop(symbol,None)
                raise
            except Exception as exc:
                from .diagnostics_log import log_event
                log_event('candle_source_unavailable',symbol=symbol,timeframe=tf,
                          error=exc,stage='candles',outcome='failed')
                if getattr(exc,'http_status',None)==429:raise
                return False
        finally:lock.release()
    return False
