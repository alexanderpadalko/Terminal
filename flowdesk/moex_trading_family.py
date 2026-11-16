"""Russian futures: Finam physical-day OHLCV assigned to MOEX trading dates.

Open comes from the first continuous Finam trade in verified session regimes;
H/L/C/V retain the native physical D values. Stocks keep native calendar D/W/M.
Intraday timestamps, futures raw bars and old native W/M archives are retained.
"""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

from .market_data import Candle, MOSCOW, candle_close_time, normalize
from .moex_calendar import CLOSED, EXTRA, REGULAR, stock_session_kind, stock_trading_date
from .provider_candles import NATIVE_HIGHER
from .session_candles import _period
from .trading_calendar import CalendarUnavailable, calendar_for

POLICY='moex-trading-date-1'
REVISION='moex-continuous-open-2026-11-16.2'
PROGRESS='MOEX_DAILY_TRADING_v1'
OPEN_PROGRESS='MOEX_CONTINUOUS_OPEN_H1_v1'
UTC=timezone.utc


def enabled(symbol):
    try:return calendar_for(symbol).profile=='MOEX_FUTURE_FULL'
    except (CalendarUnavailable,ValueError):return False


def launch(symbol):
    return date(2025,8,16)


def trading_day(day,symbol):
    if day<launch(symbol):return day
    if day.year not in (2025,2026):raise CalendarUnavailable('calendar_history_unknown')
    target=stock_trading_date(day)
    if target is None:raise CalendarUnavailable('calendar_source_conflict')
    if target.year not in (2025,2026):raise CalendarUnavailable('calendar_history_unknown')
    return target


def _local_date(bar):return bar.time.astimezone(MOSCOW).date()


def archive_start(raw,symbol):
    first=min(_period(launch(symbol),tf)[0] for tf in ('W1','MN1'))
    labels=[_local_date(b) for tf in ('W1','MN1') for b in raw.get(tf,())]
    return max(first,min(labels)) if labels else first


def archive_ready(raw,symbol):
    progress=(getattr(raw,'source_state',None) or {}).get('moex_progress')
    return bool(progress and progress[1] and
                datetime.fromisoformat(progress[0]).date()<=archive_start(raw,symbol))


def _opening_session(day,symbol):
    # Preserve the historical native prefix where continuous-session hours
    # have not been verified. Never infer old hours from today's schedule.
    try:return calendar_for(symbol).session(day)
    except CalendarUnavailable:return None


def _opening_sources(raw):
    sources={}
    for rank,tf in enumerate(('M1','M5','H1')):
        for bar in normalize(raw.get(tf,())):
            sources.setdefault(_local_date(bar),[]).append((bar.time,rank,tf,bar))
    return {day:sorted(rows,key=lambda row:row[:2]) for day,rows in sources.items()}


def _continuous_open(bar,symbol,observed,sources,coverage,observations):
    session=_opening_session(_local_date(bar),symbol)
    if session is None:return bar.open,False
    unknown=datetime.min.replace(tzinfo=UTC)
    def usable(tf,source):
        known=observations.get((tf,source.time),unknown)
        return (candle_close_time(source,tf)<=observed if known==unknown
                else source.time<=known<=observed)
    for stamp,_,tf,source in sources.get(_local_date(bar),()):
        if not session.start<=stamp<min(session.end,observed+timedelta(microseconds=1)):continue
        if not usable(tf,source):continue
        # A later first bar is safe only with an HTTP-certified empty prefix.
        # A short/age-truncated cache must not turn a later trade into Open.
        if stamp>session.start and not any(a<=session.start and b>stamp for a,b in coverage.get(tf,())):
            # Sparse contracts can leave a gap between adjacent HTTP windows.
            # Independently reconcile the ENTIRE physical native D volume and
            # H/L/C before certifying its later first trade. Mere price equality
            # or an empty response cannot prove that earlier trades are absent.
            rows=[entry[3] for entry in sources[_local_date(bar)] if entry[2]==tf
                  and session.start<=entry[0]<session.end and entry[0]<=observed and usable(tf,entry[3])]
            if not (bar.volume>0 and sum(b.volume for b in rows)==bar.volume
                    and max(b.high for b in rows)==bar.high and min(b.low for b in rows)==bar.low
                    and rows[-1].close==bar.close):continue
        if not bar.low<=source.open<=bar.high:
            raise CalendarUnavailable('opening_source_conflict')
        return source.open,True
    raise CalendarUnavailable('opening_source_unavailable')


def _first_physical_days(raw,symbol,observed):
    first={}
    for bar in normalize(raw.get('D1',())):
        if bar.time<=observed:first.setdefault(trading_day(_local_date(bar),symbol),bar)
    return first.values()


def opening_ready(raw,symbol,observed):
    state=getattr(raw,'source_state',{})
    coverage={}
    for tf,a,b in state.get('coverage',()):
        coverage.setdefault(tf,[]).append((datetime.fromisoformat(a),datetime.fromisoformat(b)))
    observations={(tf,datetime.fromisoformat(ts)):datetime.fromisoformat(stamp)
                  for tf,ts,stamp in state.get('observations',())}
    sources=_opening_sources(raw)
    try:
        for bar in _first_physical_days(raw,symbol,observed):
            _continuous_open(bar,symbol,observed,sources,coverage,observations)
    except CalendarUnavailable:return False
    return True


def _day_end(day,symbol):
    # Native D supplies H/L/C/V; Open uses the first continuous source below.
    # These deadlines control publication only, never source price selection.
    ending=time(19) if stock_session_kind(day)==EXTRA else time(23,50)
    if symbol.upper().endswith('@RTSX') and day in (date(2026,5,1),date(2026,6,12),date(2026,11,4)):
        ending=time(23,50)
    return datetime.combine(day,ending,MOSCOW).astimezone(UTC)


def _physical_end(bar,symbol):
    day=_local_date(bar)
    legacy=day.year<2026 or (symbol.upper().endswith('@RTSX') and day<date(2026,3,23))
    return candle_close_time(bar,'D1') if legacy else _day_end(day,symbol)


def _period_end(day,tf,symbol):
    finish=_period(day,tf)[1]
    for _ in range(16):
        if finish.year not in (2025,2026):raise CalendarUnavailable('calendar_history_unknown')
        if stock_session_kind(finish)==REGULAR:return _day_end(finish,symbol)
        finish-=timedelta(days=1)
    raise CalendarUnavailable('calendar_history_unknown')


def _combine(parts,label,ending):
    return Candle(parts[0].time,parts[0].open,max(b.high for b in parts),min(b.low for b in parts),
                  parts[-1].close,sum(b.volume for b in parts),scheduled_close=ending,
                  settlement_close=parts[-1].settlement_close,trading_date=label)


def _native_prefix(raw,tf,boundary,observed,observations):
    """Unused post-launch drafts must never clear the old native archive."""
    rows=normalize(raw.get(tf,()))
    unknown=datetime.min.replace(tzinfo=UTC)
    prefix=[]
    for index,bar in enumerate(rows):
        if _period(_local_date(bar),tf)[0]>=boundary:continue
        if bar.time>observed:continue
        ending=candle_close_time(bar,tf)
        known=observations.get((tf,bar.time),unknown)
        successor=index+1<len(rows) and rows[index+1].time<=observed
        if ending>observed or not ((known==unknown and successor) or ending<=known<=observed):
            return None
        prefix.append(replace(bar,scheduled_close=ending))
    return prefix


def build_family(raw,symbol,observed,*,observations,archive_verified,coverage=None):
    """Atomic higher family, or a fixed failure code. Never publish stale drafts.

    A bounded D1 archive pass must cover the entire affected native W/M suffix
    before promotion. Source days before launch and native W/M before the first
    affected period remain intact, including decades of monthly stock history.
    """
    detail={'policy':POLICY,'archive_start':archive_start(raw,symbol).isoformat()}
    if not enabled(symbol):return None,'instrument_calendar_unknown',detail
    if not archive_verified:return None,'incomplete_daily_coverage',detail
    daily=normalize(raw.get('D1',()))
    if not daily:return None,'lower_history_unavailable',detail
    unknown=datetime.min.replace(tzinfo=UTC)
    groups={}
    sources=_opening_sources(raw)
    coverage=coverage or {}
    try:
        for index,bar in enumerate(daily):
            if bar.time>observed:continue
            day=_local_date(bar);target=trading_day(day,symbol)
            # Conservative native deadline for older session regimes. This
            # avoids introducing a new pre-ETS/evening policy in this repair.
            end=_physical_end(bar,symbol)
            known=observations.get(('D1',bar.time),unknown)
            legacy=known==unknown
            successor=index+1<len(daily) and daily[index+1].time<=observed
            valid=((legacy and successor) or (not legacy and end<=known<=observed)
                   if end<=observed else not legacy and bar.time<=known<=observed)
            if not valid:return None,'native_snapshot_unconfirmed',detail
            if bar.volume<0:raise ValueError('Invalid native volume')
            groups.setdefault(target,[]).append(bar)
        days={day:(_combine(parts,day,max(_day_end(day,symbol),_physical_end(parts[-1],symbol))) if day>=launch(symbol)
                   else replace(parts[0],scheduled_close=candle_close_time(parts[0],'D1')))
              for day,parts in sorted(groups.items())}
        corrected=0;native_prefix=0
        for day,parts in groups.items():
            detail['opening_source_day']=_local_date(parts[0]).isoformat()
            price,verified=_continuous_open(parts[0],symbol,observed,sources,coverage,observations)
            days[day]=replace(days[day],open=price)
            corrected+=verified;native_prefix+=not verified
        family={'D1':list(days.values())}
        for tf in ('W1','MN1'):
            boundary=_period(launch(symbol),tf)[0]
            prefix=_native_prefix(raw,tf,boundary,observed,observations)
            if prefix is None:return None,'native_snapshot_unconfirmed',detail
            periods={}
            for day,bar in days.items():
                key=_period(day,tf)[0]
                if key>=boundary:periods.setdefault(key,[]).append(bar)
            # A real native archive period may never disappear due to short
            # D history. Empty HTTP windows alone do not certify its prices.
            expected={_period(_local_date(b),tf)[0] for b in raw.get(tf,())
                      if b.time<=observed and _period(_local_date(b),tf)[0]>=boundary}
            for missing in expected-set(periods):
                # An illiquid contract may have traded only on Sunday. Once
                # that exact source moves to Monday the old week is empty and
                # MUST disappear. Require all five provider OHLCV fields to
                # explain the removal; absence alone is never sufficient.
                source=[b for b in daily if b.time<=observed and _period(_local_date(b),tf)[0]==missing]
                labels=[b for b in raw[tf] if _period(_local_date(b),tf)[0]==missing]
                values=lambda b:(b.open,b.high,b.low,b.close,b.volume)
                if (not source or not labels or
                        values(_combine(source,missing,candle_close_time(source[-1],'D1')))!=values(labels[-1])):
                    return None,'incomplete_daily_coverage',detail
                detail.setdefault('empty_periods_transferred',[]).append((tf,missing.isoformat()))
            family[tf]=prefix+[_combine(parts,key,max(_period_end(key,tf,symbol),parts[-1].scheduled_close))
                              for key,parts in sorted(periods.items())]
    except CalendarUnavailable as exc:
        # Only fixed internal calendar codes leave this module.
        return None,exc.args[0],detail
    detail.pop('opening_source_day',None)
    detail.update(daily_count=len(days),native_archive_prefix=True,
                  opening_policy='first_continuous_trade',continuous_open_days=corrected,
                  native_open_history_days=native_prefix)
    return family,'ready',detail


def backfill(service,symbol,now,*,cancelled):
    if not enabled(symbol):return {}
    # The selected chart and scanner can request this same bootstrap at once.
    # Serialize only this instrument's bootstrap, never the DB or other charts.
    lock=service._key_lock(service._pull_locks,(symbol,PROGRESS))
    service._acquire(lock,cancelled)
    try:
        result=_backfill(service,symbol,now,cancelled=cancelled)
        if archive_ready(service._series(symbol),symbol):
            result.update(_backfill_openings(service,symbol,now,cancelled=cancelled))
        return result
    finally:lock.release()


def _backfill(service,symbol,now,*,cancelled):
    """Four initial <=180-day D windows, then normal overlapping refreshes.

    Explicit old-date requests are supported by Finam; no H1 archive rebuild
    and no writes to the original provider bars beyond normal cache upserts.
    Each cursor is durable, cancellation is checked around every request.
    """
    from .session_store import record_response
    raw=service._series(symbol)
    if not any(raw.get(tf) for tf in NATIVE_HIGHER):return {}
    start=datetime.combine(archive_start(raw,symbol),time.min,UTC)
    progress=service.load_history_progress().get((symbol,PROGRESS),{})
    if progress.get('exhausted') and progress.get('cursor',now)<=start:return {}
    cursor=min(now,progress.get('cursor',now))
    for _ in range(4):
        if cursor<=start:break
        if cancelled():raise InterruptedError()
        left=max(start,cursor-timedelta(days=180))
        try:bars=service.api.bars(symbol,'D1',start=left,end=cursor)
        except InterruptedError:raise
        except Exception as exc:
            from .diagnostics_log import log_event
            log_event('candle_source_unavailable',symbol=symbol,timeframe='D1',
                      error=exc,stage='candles',outcome='failed')
            raise
        if cancelled():raise InterruptedError()
        if not bars and any(left<=b.time<cursor for b in raw.get('D1',())):
            return {'D1':'исходное окно Finam не подтверждено'}
        if bars:service._upsert(symbol,'D1',bars)
        record_response(service,symbol,'D1',bars,left,cursor,now)
        cursor=left
        service.save_history_progress(symbol,PROGRESS,{'cursor':cursor,'exhausted':cursor<=start})
    return {'D1':'проверка дневной истории торговых дней'}


def _backfill_openings(service,symbol,now,*,cancelled):
    """Bounded H1 history for Open only, shared by chart and scanner bootstrap.

    Native H/L/C/V and raw bars remain untouched. Exact session-start H1 Open
    is sufficient: it cannot contain the auction from the preceding hour.
    Existing M1/M5 may supply a finer first continuous trade without HTTP.
    """
    from .session_store import record_response
    raw=service._series(symbol)
    if opening_ready(raw,symbol,now):return {}
    starts=[session.start for bar in _first_physical_days(raw,symbol,now)
            if (session:=_opening_session(_local_date(bar),symbol)) is not None]
    if not starts:return {}
    start=min(starts)
    progress=service.load_history_progress().get((symbol,OPEN_PROGRESS),{})
    cursor=min(now,progress.get('cursor',now))
    # The normal H1 refresh supplies the live right edge. Do not repeatedly
    # download a completed archive when the provider has no opening source.
    if cursor<=start:return {}
    for _ in range(8):
        if cursor<=start or opening_ready(service._series(symbol),symbol,now):break
        if cancelled():raise InterruptedError()
        left=max(start,cursor-timedelta(days=30))
        try:bars=service.api.bars(symbol,'H1',start=left,end=cursor)
        except InterruptedError:raise
        except Exception as exc:
            from .diagnostics_log import log_event
            log_event('candle_source_unavailable',symbol=symbol,timeframe='H1',
                      error=exc,stage='candles',outcome='failed')
            raise
        if cancelled():raise InterruptedError()
        if bars:service._upsert(symbol,'H1',bars)
        record_response(service,symbol,'H1',bars,left,cursor,now)
        cursor=left
        service.save_history_progress(symbol,OPEN_PROGRESS,{'cursor':cursor,'exhausted':cursor<=start})
    return {'H1':'проверка открытия непрерывных торгов'}
