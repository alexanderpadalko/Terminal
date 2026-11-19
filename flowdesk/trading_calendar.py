"""Bounded, source-backed trading sessions, separate from VWAP and expiry.

No weekday fallback is treated as a verified calendar. Unknown historical
regimes and unverified holiday hours raise CalendarUnavailable. A dated,
product-specific override can extend coverage without changing aggregation.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import re
from zoneinfo import ZoneInfo

from .moex_calendar import CLOSED, EXTRA, stock_session_kind

UTC=timezone.utc
CALENDAR_VERSION='sessions-2026-11-15.2'


class CalendarUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class TradingSession:
    trading_date: date
    segments: tuple[tuple[datetime,datetime], ...]

    def __post_init__(self):
        if not self.segments:
            raise ValueError('Empty trading session')
        if any(a.tzinfo is None or b.tzinfo is None for a,b in self.segments):
            raise ValueError('Session timestamps must be timezone aware')
        object.__setattr__(self,'segments',tuple((a.astimezone(UTC),b.astimezone(UTC)) for a,b in self.segments))
        previous=None
        for start,end in self.segments:
            if (start.tzinfo is None or end.tzinfo is None or start>=end or
                    (previous is not None and start<previous)):
                raise ValueError('Invalid trading session')
            previous=end

    @property
    def start(self):return self.segments[0][0]
    @property
    def end(self):return self.segments[-1][1]

    def buckets(self,minutes):
        """Consume actual trading minutes across breaks; never cross a session."""
        if type(minutes) is not int or minutes<=0:raise ValueError('Invalid bucket size')
        remaining=timedelta(minutes=minutes)
        chunk=[]
        for start,end in self.segments:
            while start<end:
                stop=min(end,start+remaining)
                chunk.append((start,stop))
                remaining-=stop-start
                start=stop
                if not remaining:
                    yield tuple(chunk)
                    chunk=[];remaining=timedelta(minutes=minutes)
        if chunk:yield tuple(chunk)


_US_HOLIDAYS={
    2026:('01-01','01-19','02-16','04-03','05-25','06-19','07-03','09-07','11-26','12-25'),
    2027:('01-01','01-18','02-15','03-26','05-31','06-18','07-05','09-06','11-25','12-24'),
    2028:('01-17','02-21','04-14','05-29','06-19','07-04','09-04','11-23','12-25'),
}
_US_EARLY={2026:('11-27','12-24'),2027:('11-26',),2028:('07-03','11-24')}
# CME's public holiday page lists affected ranges but the interactive product
# hours could not be independently retrieved. Never substitute NYSE closures,
# settlement times, ClearPort or another CME product's hours for Globex.
_CME_UNKNOWN_2026=((date(2026,1,1),date(2026,1,2)),(date(2026,1,18),date(2026,1,20)),
    (date(2026,2,15),date(2026,2,17)),(date(2026,4,2),date(2026,4,4)),
    (date(2026,5,24),date(2026,5,26)),(date(2026,6,18),date(2026,6,19)),
    (date(2026,7,3),date(2026,7,5)),(date(2026,9,6),date(2026,9,8)),
    (date(2026,11,25),date(2026,11,28)),(date(2026,12,23),date(2026,12,26)),
    (date(2026,12,30),date(2026,12,31)))


@dataclass(frozen=True)
class TradingCalendar:
    profile: str
    zone_name: str
    root: str=''
    # Overrides are UTC interval tuples keyed by trading date. None = closed.
    overrides: tuple=()

    @property
    def revision(self):return (CALENDAR_VERSION,self.profile,self.zone_name,self.root,self.overrides)
    @property
    def zone(self):return ZoneInfo(self.zone_name)

    def _session(self,day,opening,closing,*,previous=False):
        start=datetime.combine(day-timedelta(days=bool(previous)),opening,self.zone).astimezone(UTC)
        end=datetime.combine(day,closing,self.zone).astimezone(UTC)
        return TradingSession(day,((start,end),))

    @lru_cache(maxsize=16384)
    def session(self,day):
        overrides=dict(self.overrides)
        if day in overrides:
            return TradingSession(day,tuple(overrides[day])) if overrides[day] else None
        md=day.strftime('%m-%d')
        if self.profile=='CRYPTO_UTC':
            start=datetime.combine(day,time.min,UTC)
            return TradingSession(day,((start,start+timedelta(days=1)),))
        if self.profile=='US_EQUITY_RTH':
            if day.year not in _US_HOLIDAYS:raise CalendarUnavailable('calendar_history_unknown')
            if day.weekday()>=5 or md in _US_HOLIDAYS[day.year]:return None
            return self._session(day,time(9,30),time(13) if md in _US_EARLY[day.year] else time(16))
        if self.profile in ('MOEX_STOCK_FULL','MOEX_FUTURE_FULL'):
            # Full CONTINUOUS session; auctions are not an H4 anchor. Existing
            # source-level closing-auction exclusion remains independent.
            if day.year!=2026:raise CalendarUnavailable('calendar_history_unknown')
            kind=stock_session_kind(day)
            if kind==CLOSED:return None
            if self.profile=='MOEX_FUTURE_FULL' and day<date(2026,3,23):
                raise CalendarUnavailable('pre_ets_schedule_unknown')
            if kind==EXTRA:
                if (self.profile=='MOEX_FUTURE_FULL' and self.root in ('SI','EU','CR','ED','USDRUBF','CNYRUBF','EURRUBF')
                        and day<date(2026,7,18)):
                    raise CalendarUnavailable('weekend_product_admission_unknown')
                end=time(23,50) if self.profile=='MOEX_FUTURE_FULL' and md in ('05-01','06-12','11-04') else time(19)
                return self._session(day,time(10),end)
            opening=time(9) if self.profile=='MOEX_FUTURE_FULL' and day<date(2026,7,14) else time(7)
            return self._session(day,opening,time(23,50))
        if self.profile.startswith('CME_'):
            if day.year!=2026:raise CalendarUnavailable('calendar_history_unknown')
            if day.weekday()>=5:return None
            if any(start<=day<=end for start,end in _CME_UNKNOWN_2026):
                raise CalendarUnavailable('product_holiday_hours_unknown')
            return self._session(day,time(17),time(16),previous=True)
        if self.profile=='ICE_BRENT':
            if day.year!=2026:raise CalendarUnavailable('calendar_history_unknown')
            if day.weekday()>=5 or md in ('01-01','04-03','12-25'):return None
            # Verified October temporary schedule. March circular must be
            # supplied separately; do not infer it from the autumn circular.
            if date(2026,3,8)<=day<=date(2026,3,29):
                raise CalendarUnavailable('dst_product_hours_unknown')
            temporary=date(2026,10,26)<=day<=date(2026,10,30)
            opening=time(0) if temporary else time(1)
            closing=time(22) if temporary else time(23)
            if md in ('05-25','06-19','07-03','09-07','11-26'):closing=time(18,30)
            if md=='11-27':closing=time(20)
            if md in ('12-24','12-28','12-31'):
                raise CalendarUnavailable('product_holiday_hours_unknown')
            if day.weekday()==0:
                return self._session(day,time(22) if temporary else time(23),closing,previous=True)
            return self._session(day,opening,closing)
        raise CalendarUnavailable('instrument_calendar_unknown')

    def sessions(self,start,end):
        cursor=start
        while cursor<=end:
            session=self.session(cursor)
            if session is not None:yield session
            cursor+=timedelta(days=1)

    def trading_date(self,physical_day):
        """MOEX additional sessions belong to the next REGULAR trading day.

        Keep physical sessions separate for H1/H4; this mapping is for D/W/M.
        It is independent of the post-ETS evening/current-day rule.
        """
        if self.profile not in ('MOEX_STOCK_FULL','MOEX_FUTURE_FULL'):
            return physical_day
        if physical_day.year!=2026:raise CalendarUnavailable('calendar_history_unknown')
        if stock_session_kind(physical_day)!=EXTRA:return physical_day
        candidate=physical_day+timedelta(days=1)
        while candidate.year==2026:
            if stock_session_kind(candidate) not in (EXTRA,CLOSED):return candidate
            candidate+=timedelta(days=1)
        raise CalendarUnavailable('calendar_history_unknown')

    @lru_cache(maxsize=16384)
    def daily_session(self,day):
        if day in dict(self.overrides):return self.session(day)
        if self.profile not in ('MOEX_STOCK_FULL','MOEX_FUTURE_FULL'):
            return self.session(day)
        if day.year!=2026:raise CalendarUnavailable('calendar_history_unknown')
        if stock_session_kind(day) in (EXTRA,CLOSED):return None
        regular=self.session(day)
        if regular is None:return None
        pieces=list(regular.segments)
        prior=day-timedelta(days=1)
        while prior.year==2026 and stock_session_kind(prior) in (EXTRA,CLOSED):
            if stock_session_kind(prior)==EXTRA:
                extra=self.session(prior)
                if extra is not None:pieces[0:0]=extra.segments
            prior-=timedelta(days=1)
        # A year-boundary session needs the preceding year's verified dates.
        if prior.year!=2026:raise CalendarUnavailable('calendar_history_unknown')
        return TradingSession(day,tuple(pieces))

    def daily_sessions(self,start,end):
        cursor=start
        while cursor<=end:
            session=self.daily_session(cursor)
            if session is not None:yield session
            cursor+=timedelta(days=1)


@lru_cache(maxsize=256)
def calendar_for(symbol):
    ticker,mic=symbol.upper().rsplit('@',1)
    if mic=='BINANCE' and ticker in ('BTCUSDT','ETHUSDT'):
        return TradingCalendar('CRYPTO_UTC','UTC')
    if mic in ('MISX','XMOE'):
        return TradingCalendar('MOEX_STOCK_FULL','Europe/Moscow')
    match=re.fullmatch(r'([A-Z][A-Z0-9]*)[FGHJKMNQUVXZ][0-9]{1,4}',ticker)
    root=match[1] if match else {'USDRUB.P':'USDRUBF','CNYRUB.P':'CNYRUBF',
        'EURRUB.P':'EURRUBF','IRUS.P':'IMOEXF'}.get(ticker,ticker)
    if mic=='RTSX' and root in ('SI','EU','CR','ED','MX','MIX','RI','USDRUBF','CNYRUBF','EURRUBF','IMOEXF',
                               'GD','SV','BR','NG','MM','GN','S1','BM','NR'):
        return TradingCalendar('MOEX_FUTURE_FULL','Europe/Moscow',root)
    if mic=='XCME' and root in ('NQ','ES'):
        return TradingCalendar('CME_EQUITY','America/Chicago',root)
    if mic in ('XCEC','XCME') and root in ('GC','SI'):
        return TradingCalendar('CME_METALS','America/Chicago',root)
    if mic in ('XNYM','XCME') and root in ('PL','PA','NG'):
        return TradingCalendar('CME_ENERGY' if root=='NG' else 'CME_METALS','America/Chicago',root)
    # BRN at XNYM/XICE is deliberately not assumed to be ICE Futures Europe.
    if mic=='IFEU' and root=='BRN':return TradingCalendar('ICE_BRENT','Europe/London',root)
    if mic in ('XNGS','XNMS','XNCM','XNAS','XNYS','ARCX','XASE'):
        return TradingCalendar('US_EQUITY_RTH','America/New_York')
    raise CalendarUnavailable('instrument_calendar_unknown')
