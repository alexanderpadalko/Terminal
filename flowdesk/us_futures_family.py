"""Curated US futures: complete Finam H1 session OHLCV, Last-price D/W/M.

Raw provider archives are never modified. A candidate must cover every observed
session in the available archive before the whole higher family can replace
native D/W/M. The initial, truncated source session is explicitly identified.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re

from .market_data import candle_close_time, normalize
from .session_candles import _LowerSource, _combine, _period
from .trading_calendar import TradingCalendar, TradingSession, CalendarUnavailable

UTC=timezone.utc
POLICY='us-futures-last-trade-1'
PRODUCTS={
    'NQ':('CME_EQUITY',('XCME',)), 'ES':('CME_EQUITY',('XCME',)),
    'GC':('CME_METALS',('XCEC','XCME')), 'SI':('CME_METALS',('XCEC','XCME')),
    'PL':('CME_METALS',('XNYM','XCME')), 'PA':('CME_METALS',('XNYM','XCME')),
    'NG':('CME_ENERGY',('XNYM','XCME')), 'BRN':('ICE_BRENT',('IFEU','XICE','XNYM')),
}
# CME Globex / published broker session notices; checked 2026-11-16.
# June/July stop at 12 CT for these products. Labor Day and Thanksgiving
# have different Equity vs Energy/Metals hours. See the source audit.
# Unknown earlier/future holiday regimes remain unavailable, never inferred.
HOLIDAYS={date(2026,6,19):date(2026,6,22),
          date(2026,7,3):date(2026,7,6),
          date(2026,9,7):date(2026,9,8),
          date(2026,11,26):date(2026,11,27)}


@dataclass(frozen=True)
class FuturesCalendar(TradingCalendar):
    @property
    def revision(self):return (POLICY,super().revision)

    def session(self,day):
        if day in dict(self.overrides):return super().session(day)
        if self.profile.startswith('CME_'):
            if not date(2026,6,1)<=day<=date(2026,11,30):
                return super().session(day)  # Keeps unknown holiday guards.
            if day.weekday()>=5:return None
            closing=time(16)
            if day in HOLIDAYS:
                closing=(time(13,30) if self.profile!='CME_EQUITY' and
                         day in (date(2026,9,7),date(2026,11,26)) else time(12))
            if day==date(2026,11,27):
                # Energy/Metals date is not independently verified here.
                if self.profile!='CME_EQUITY':
                    raise CalendarUnavailable('product_holiday_hours_unknown')
                closing=time(12,15)
            return self._session(day,time(17),closing,previous=True)
        return super().session(day)

    def trading_date(self,day):
        return HOLIDAYS.get(day,day) if self.profile.startswith('CME_') else day

    def daily_session(self,day):
        if day in dict(self.overrides):return self.session(day)
        if not self.profile.startswith('CME_'):return super().daily_session(day)
        if day in HOLIDAYS:return None
        regular=self.session(day)
        if regular is None:return None
        prior=next((holiday for holiday,target in HOLIDAYS.items() if target==day),None)
        if prior is None:return regular
        return TradingSession(day,self.session(prior).segments+regular.segments)


def product(symbol):
    match=re.fullmatch(r'([A-Z]+)[FGHJKMNQUVXZ][0-9]{1,4}@([A-Z0-9]+)',symbol.upper())
    if not match:return None
    root,mic=match.groups();spec=PRODUCTS.get(root)
    return root if spec and mic in spec[1] else None


def enabled(symbol,calendar):
    root=product(symbol)
    return bool(root and calendar.root==root and calendar.profile==PRODUCTS[root][0]
                and (root!='BRN' or symbol.upper().endswith('@IFEU')))


def calendar_for_family(calendar):
    return FuturesCalendar(calendar.profile,calendar.zone_name,calendar.root,calendar.overrides)


def build_family(raw,calendar,observed,*,coverage,observations):
    """Return (whole candidate or None, fixed diagnostic status, metadata).

    H1 is used for the entire archive so volume units are not mixed with M5.
    Source gaps require bounded HTTP evidence; a stale draft cannot be sealed
    by the wall clock. Close is the last H1 price INSIDE the same session.
    Native D/W/M supply archive boundaries only, never prices or settlement.
    No external price feed is used.
    """
    rows=normalize(raw.get('H1',()))
    native=normalize(raw.get('D1',()))
    detail={'policy':POLICY,'close_rule':'session_last_trade',
            'volume_rule':'finam_h1_sum','left_partial':False}
    if not rows or not native:return None,'lower_history_unavailable',detail
    if any(b.volume<0 or b.settlement_close for b in rows):
        return None,'control_source_invalid',detail
    # Native labels may be several hours in the future while the exchange's
    # evening session is already trading. Labels anchor the archive only;
    # their OHLC and observation stamps never determine the session Close.
    archive_days={b.time.astimezone(UTC).date() for b in native}
    if any(observations.get(('H1',b.time),observed)>observed for b in rows if b.time<observed):
        return None,'incomplete_lower_coverage',detail
    first=rows[0].time
    # Do not silently discard an older native month while rebuilding a short
    # recent suffix. Daily date labels can precede the first actual H1 by one
    # calendar day; only that left boundary may be partial.
    first_day=min(archive_days)
    if first.astimezone(calendar.zone).date()>first_day+timedelta(days=1):
        return None,'incomplete_lower_coverage',detail
    source=_LowerSource({'H1':rows},coverage,observations,observed,calendar)
    days={};cursor=first_day;last=observed.astimezone(calendar.zone).date()+timedelta(days=1)
    try:
        while cursor<=last:
            session=calendar.daily_session(cursor);cursor+=timedelta(days=1)
            if session is None or session.start>=observed or session.end<=first:continue
            pieces=session.segments
            if pieces[0][0]<first:
                # Preserve the available initial month, without inventing
                # earlier prices or representing it as a complete session.
                pieces=tuple((max(a,first),b) for a,b in pieces if b>first)
                detail['left_partial']=True
                detail['available_from']=first.isoformat()
            bar,origin=source.bucket(pieces,session.end,
                                    verified_closes=tuple(b for _,b in pieces))
            if bar is None:
                if origin=='no_trades':continue
                detail['blocked_date']=session.trading_date.isoformat()
                return None,'incomplete_lower_coverage',detail
            # _LowerSource already requires a post-close H1 snapshot and
            # excludes later-session bars. Do not replace its C with native
            # D1: historical native C can contain the NEXT evening's trades.
            days[session.trading_date]=bar
        if not days:return None,'lower_history_unavailable',detail
        family={'D1':list(days.values())}
        for tf in ('W1','MN1'):
            periods=sorted({_period(day,tf) for day in days});higher=[]
            for begin,finish in periods:
                part=[b for d,b in days.items() if begin<=d<=finish]
                # Determine true period close even while it is still forming.
                tail=finish
                try:
                    while calendar.daily_session(tail) is None:tail-=timedelta(days=1)
                    end=calendar.daily_session(tail).end
                except CalendarUnavailable:
                    # An unknown FUTURE holiday at a forming period's end
                    # cannot remove today's known trades. Use the provider's
                    # later nominal deadline conservatively; no closed period
                    # and no unknown observed session is accepted this way.
                    labels=[b for b in raw.get(tf,())
                            if _period(b.time.astimezone(UTC).date(),tf)==(begin,finish)]
                    if finish<=observed.astimezone(calendar.zone).date() or not labels:raise
                    end=candle_close_time(labels[-1],tf)
                    if end<=observed:raise
                    detail.setdefault('provisional_period_closes',[]).append(tf)
                higher.append(_combine(part[0].time,end,part))
            family[tf]=higher
    except CalendarUnavailable:
        return None,'product_holiday_hours_unknown',detail
    # All native archive periods must survive promotion; timestamps are date
    # labels in native bars and actual session openings in the new family.
    for tf in ('W1','MN1'):
        expected={_period(b.time.astimezone(UTC).date(),tf) for b in raw.get(tf,()) if b.time<=observed}
        actual={_period(d,tf) for d in days}
        if not expected.issubset(actual):return None,'incomplete_daily_coverage',detail
    detail['daily_count']=len(days)
    return family,'ready',detail


def next_window(raw,now,progress,calendar):
    """Newest unverified interior H1 window, not min(raw timestamp).

    A persisted successful coverage interval survives restart. An empty or
    truncated response cannot certify the missing interior. At most 30 days
    are requested per scanner pass through the existing shared coordinator.
    """
    native=raw.get('D1',())
    if not native:return None
    start=min(b.time for b in native)-timedelta(days=1)
    last=max(b.time.astimezone(UTC).date() for b in native)
    try:end=min(now,calendar.daily_session(calendar.trading_date(last)).end)
    except (CalendarUnavailable,AttributeError):return None
    # A versioned cursor revalidates the entire existing archive, including
    # bars in the middle which predate observation/coverage bookkeeping.
    cursor=progress.get('cursor')
    if not progress.get('exhausted'):
        end=min(end,cursor) if cursor is not None else end
        return (max(start,end-timedelta(days=30)),end) if start<end else None
    # Subsequent passes repair actual holes/new sessions. The initial
    # truncated boundary has already been probed by a bounded HTTP request.
    if raw.get('H1'):start=max(start,raw['H1'][0].time)
    windows=sorted((datetime.fromisoformat(a),datetime.fromisoformat(b))
                   for tf,a,b in raw.source_state['coverage'] if tf=='H1')
    from .session_candles import _covered
    try:
        sessions=list(calendar.daily_sessions(start.astimezone(calendar.zone).date(),
                                              end.astimezone(calendar.zone).date()+timedelta(days=1)))
    except CalendarUnavailable:return None
    for session in reversed(sessions):
        for left,right in reversed(session.segments):
            left=max(left,start);right=min(right,end)
            if left<right and not _covered(windows,left,right):
                return max(start,right-timedelta(days=30)),right
    return None


def backfill(service,symbol,now,*,cancelled):
    from .session_store import record_response
    raw=service._series(symbol)
    key='US_FUTURES_H1_v1'
    progress=service.load_history_progress().get((symbol,key),{})
    cal=calendar_for_family(service.calendar_factory(symbol))
    # One initial bounded batch (six 30-day windows), then one repair window
    # per pass. Each completed request persists its cursor before continuing.
    budget=1 if progress.get('exhausted') else 6
    for _ in range(budget):
        window=next_window(raw,now,progress,cal)
        if window is None:return {}
        if cancelled():raise InterruptedError()
        start,end=window
        bars=service.api.bars(symbol,'H1',start=start,end=end)
        if cancelled():raise InterruptedError()
        if not bars:return {'D1':'исходное окно Finam не подтверждено'}
        service._upsert(symbol,'H1',bars)
        record_response(service,symbol,'H1',bars,start,end,now)
        earliest=min(b.time for b in raw['D1'])-timedelta(days=1)
        progress={'cursor':start,'exhausted':progress.get('exhausted',False) or start<=earliest}
        service.save_history_progress(symbol,key,progress)
        if progress['exhausted']:break
        raw=service._series(symbol)
    return {'D1':'проверка исходной истории фьючерса'}
