"""Session OHLCV from same-provider lower bars; no synthetic missing prices.

Raw higher provider candles never enter a partially rebuilt family. Native
daily OHLCV has priority when it matches a complete same-session lower source;
Binance UTC H4/D1 are independently identifiable native intervals.
"""
from bisect import bisect_left
from dataclasses import dataclass, field,replace
from datetime import date, datetime, time, timedelta, timezone
from math import isclose

from .market_data import Candle, candle_close_time, normalize
from .trading_calendar import CalendarUnavailable

UTC=timezone.utc
AGGREGATION_VERSION='session-ohlcv-1'
LOWER=('M1','M5','H1')
HIGHER=('H4','D1','W1','MN1')


@dataclass
class SessionSeries:
    series: dict
    issues: dict=field(default_factory=dict)
    origins: dict=field(default_factory=dict)
    version: str=AGGREGATION_VERSION


def _combine(start,end,bars):
    return Candle(start,bars[0].open,max(b.high for b in bars),min(b.low for b in bars),
                  bars[-1].close,sum(b.volume for b in bars),scheduled_close=end)


def _covered(intervals,start,end):
    cursor=start
    for left,right in intervals:
        if right<=cursor:continue
        if left>cursor:return False
        cursor=max(cursor,right)
        if cursor>=end:return True
    return cursor>=end


class _LowerSource:
    def __init__(self,series,coverage,observed_at,observed,calendar):
        self.series={tf:normalize(series.get(tf,[])) for tf in LOWER}
        for tf,bars in self.series.items():
            limit=timedelta(minutes={'M1':1,'M5':5,'H1':60}[tf])
            if any(candle_close_time(b,tf).astimezone(UTC)-b.time.astimezone(UTC)>limit for b in bars):
                raise ValueError('Invalid source candle interval')
        self.times={tf:[b.time for b in bars] for tf,bars in self.series.items()}
        self.coverage=coverage
        self.observed_at=observed_at
        self.observed=observed
        self.calendar=calendar

    def bucket(self,pieces,session_end):
        """Use one complete source TF; never cut an already aggregated price."""
        start,end=pieces[0][0],pieces[-1][1]
        if start>=self.observed:return None,None
        needed=[(a,min(b,self.observed)) for a,b in pieces if a<self.observed]
        for tf in LOWER:
            bars=self.series[tf];times=self.times[tf]
            if not bars:continue
            if tf=='H1' and self.calendar.profile=='MOEX_STOCK_FULL':
                # The application's existing source filter excludes the
                # weekday closing auction. A mixed native hourly OHLC cannot
                # be purged by removing five minutes from its timestamp.
                unsafe=False
                for a,b in needed:
                    local=a.astimezone(self.calendar.zone)
                    if local.date()>=date(2026,3,23) and local.weekday()<5:
                        auction=datetime.combine(local.date(),time(18,55),self.calendar.zone).astimezone(UTC)
                        if a<auction+timedelta(minutes=5) and b>auction:unsafe=True
                if unsafe:continue
            collected=[];valid=True
            for left,right in needed:
                index=bisect_left(times,left)
                if index and candle_close_time(bars[index-1],tf)>left:
                    valid=False;break
                selected=[];tiles=[]
                while index<len(bars) and bars[index].time<right:
                    bar=bars[index];index+=1
                    ending=candle_close_time(bar,tf)
                    # A final hourly source can end nominally after a verified
                    # exchange close. It contains no later session's trades.
                    if ending>right:
                        if right==session_end:
                            ending=right
                        elif right==self.observed and self.observed<end:
                            known=self.observed_at.get((tf,bar.time))
                            if known is None or known<bar.time or known>self.observed:
                                valid=False;break
                            ending=right
                        else:
                            valid=False;break
                    if ending<=bar.time:
                        valid=False;break
                    known=self.observed_at.get((tf,bar.time))
                    closed=(candle_close_time(bar,tf)<=self.observed or
                            (right==session_end and right<=self.observed))
                    if closed and known is not None and known<ending:
                        valid=False;break
                    selected.append(bar);tiles.append((bar.time,ending))
                if not valid:break
                # Coverage is an explicit successful HTTP interval, not an
                # inference from the oldest/newest returned trade. Sparse
                # no-trade periods need this proof; otherwise every slot must
                # be tiled. No zero-price or forward-filled bars are made.
                intervals=sorted(self.coverage.get(tf,()))
                if not (_covered(tiles,left,right) or _covered(intervals,left,right)):
                    valid=False;break
                collected.extend(selected)
            if valid:
                return (_combine(start,end,collected),tf) if collected else (None,'no_trades')
        return None,None


def _same_prices(a,b):
    return all(isclose(x,y,rel_tol=1e-10,abs_tol=1e-8) for x,y in
               zip((a.open,a.high,a.low,a.close,a.volume),(b.open,b.high,b.low,b.close,b.volume)))


def _period(day,tf):
    if tf=='W1':
        first=day-timedelta(days=day.weekday());return first,first+timedelta(days=6)
    first=day.replace(day=1)
    following=first.replace(year=first.year+(first.month==12),month=first.month%12+1)
    return first,following-timedelta(days=1)


def build_session_series(raw,calendar,observed,*,coverage=None,observed_at=None):
    """Deterministic full/repeated/incremental result for one source snapshot.

    A period missing its start, trading day, calendar or lower coverage is
    omitted and diagnosed. Raw data remains in the separate provider store.
    Future source bars/unknown forming snapshots cannot leak into OHLC.
    """
    if observed.tzinfo is None:raise ValueError('Observation must be timezone aware')
    observed=observed.astimezone(UTC)
    if any(b.volume<0 for bars in raw.values() for b in bars):
        raise ValueError('Invalid source volume')
    coverage=coverage or {};observed_at=observed_at or {}
    output={tf:normalize(bars) for tf,bars in raw.items() if tf not in (*HIGHER,'H1')}
    issues={};origins={tf:'session_lower' for tf in HIGHER}
    lower=_LowerSource(raw,coverage,observed_at,observed,calendar)
    starts=[b.time for tf in LOWER for b in lower.series[tf] if b.time<=observed]
    intraday_start=min(starts) if starts else observed
    crypto=calendar.profile=='CRYPTO_UTC'
    if crypto:starts.extend(b.time for tf in ('H4','D1') for b in raw.get(tf,[]) if b.time<=observed)
    if not starts:
        return SessionSeries(output,{tf:'lower_history_unavailable' for tf in HIGHER},origins)
    first=min(starts).astimezone(calendar.zone).date()
    last=observed.astimezone(calendar.zone).date()+timedelta(days=1)
    native_daily={b.time.astimezone(UTC).date():b for b in normalize(raw.get('D1',[]))}
    native_crypto={tf:{b.time:b for b in normalize(raw.get(tf,[]))} for tf in ('H4','D1')}
    h4_start=min(intraday_start,min(native_crypto['H4'],default=observed))
    days={};empty_days=set();h4=[];hourly=[]
    def native_bar(tf,start,end):
        bar=native_crypto[tf].get(start)
        if (bar is not None and (bar.scheduled_close is None or bar.scheduled_close==end) and
                ((end<=observed and observed_at.get((tf,bar.time),end)>=end) or
                 (end>observed and start<=observed_at.get((tf,bar.time),observed+timedelta(days=1))<=observed))):
            return replace(bar,scheduled_close=end)
        return None
    cursor=first
    while cursor<=last:
        try:session=calendar.session(cursor)
        except CalendarUnavailable as exc:
            if cursor<=observed.astimezone(calendar.zone).date():
                for tf in HIGHER:issues.setdefault(tf,str(exc))
                h4.clear();hourly.clear();days.clear();empty_days.clear()
            cursor+=timedelta(days=1);continue
        cursor+=timedelta(days=1)
        if session is None or session.start>=observed:continue
        if session.end>intraday_start:
            for pieces in session.buckets(60):
                if pieces[0][0]>=observed:continue
                hour,origin=lower.bucket(pieces,session.end)
                if hour is not None:hourly.append(hour)
                elif origin!='no_trades' and pieces[-1][1]<=observed:hourly.clear()
        if session.end<=h4_start:pieces_iter=()
        else:pieces_iter=session.buckets(240)
        for pieces in pieces_iter:
            if pieces[0][0]>=observed:continue
            bar=native_bar('H4',pieces[0][0],pieces[-1][1]) if crypto else None
            origin=None
            if bar is None:bar,origin=lower.bucket(pieces,session.end)
            if bar is not None:h4.append(bar)
            elif origin=='no_trades':continue
            else:
                issues.setdefault('H4','incomplete_lower_coverage')
                if pieces[-1][1]<=observed:h4.clear()
        day=native_bar('D1',session.start,session.end) if crypto else None
        origin='provider_native_verified' if day is not None else 'session_lower'
        lower_origin=None
        if day is None:day,lower_origin=lower.bucket(session.segments,session.end)
        if day is None:
            if lower_origin=='no_trades':empty_days.add(session.trading_date);continue
            issues.setdefault('D1','incomplete_lower_coverage')
            if session.end<=observed:days.clear();empty_days.clear()
            continue
        official=native_daily.get(session.trading_date)
        # Native provider labels are date labels, not an instruction to anchor
        # the next exchange session at that timestamp. Only reuse its prices
        # after same-session equality, including comparable volume, is proven.
        if not crypto and official is not None and session.end<=observed:
            if _same_prices(official,day):
                day=Candle(session.start,official.open,official.high,official.low,
                           official.close,official.volume,scheduled_close=session.end)
                origin='provider_native_verified'
            else:issues['D1']='native_daily_session_mismatch'
        days[session.trading_date]=day
        origins['D1']=origin if origins['D1']==origin else 'session_lower_or_verified_daily'
    output['H1']=normalize(hourly);output['H4']=normalize(h4);output['D1']=normalize(list(days.values()))
    for tf in ('W1','MN1'):
        assembled=[]
        periods=sorted({_period(day,tf) for day in days})
        for begin,finish in periods:
            try:
                # Unknown future interior holidays do not invalidate today's
                # provisional OHLC if both period boundaries are verified.
                # They must be known before their own trades can be included.
                left=begin;right=finish;first_session=last_session=None
                while left<=finish and first_session is None:
                    first_session=calendar.session(left);left+=timedelta(days=1)
                while right>=begin and last_session is None:
                    last_session=calendar.session(right);right-=timedelta(days=1)
                through=min(finish,observed.astimezone(calendar.zone).date()+timedelta(days=1))
                sessions=list(calendar.sessions(begin,through))
            except CalendarUnavailable as exc:
                issues.setdefault(tf,str(exc));continue
            needed=[s for s in sessions if s.start<observed and s.trading_date not in empty_days]
            if not needed:continue
            if any(s.trading_date not in days for s in needed):
                issues.setdefault(tf,'incomplete_daily_coverage');continue
            chunk=[days[s.trading_date] for s in needed]
            if first_session is None or last_session is None:continue
            assembled.append(_combine(first_session.start,last_session.end,chunk))
        output[tf]=normalize(assembled)
        origins[tf]='session_daily'
    # An incomplete latest source must not masquerade as a current signal
    # from an earlier cached period. Retain historical drawing only when the
    # currently required closed period is present (caller records issues).
    return SessionSeries({tf:bars for tf,bars in output.items() if bars},issues,origins)
