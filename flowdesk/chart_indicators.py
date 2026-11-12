"""Qt-independent VWAP and current instrument summary for the pinned settings."""
from bisect import bisect_right
from .market_data import MOSCOW,candle_close_time


def period_key(dt,anchor):
    dt=dt.astimezone(MOSCOW)
    if anchor=='Session':return dt.date()
    if anchor=='Week':return dt.isocalendar()[:2]
    if anchor=='Month':return dt.year,dt.month
    if anchor=='Quarter':return dt.year,(dt.month-1)//3
    if anchor=='Year':return dt.year
    if anchor=='Decade':return dt.year//10
    if anchor=='Century':return dt.year//100
    raise ValueError(anchor)


def vwap_series(candles,anchor='Session',source='hlc3'):
    values=[]
    previous=[]
    key=None
    pv=volume=0.0
    old=None
    segment=None
    for i,bar in enumerate(candles):
        next_key=period_key(bar.time,anchor)
        reset=key!=next_key
        if i and anchor in ('Decade','Century'):
            current_year=bar.time.astimezone(MOSCOW).year
            prior_year=candles[i-1].time.astimezone(MOSCOW).year
            reset=current_year!=prior_year and current_year%(10 if anchor=='Decade' else 100)==0
        if i==0 or reset:
            if i and values[-1] is not None:
                old=values[-1]
                segment=[i,len(candles),old]
                if previous:previous[-1][1]=i
                previous.append(segment)
            else:
                old=None
                if previous:previous[-1][1]=i
            pv=volume=0.0
            key=next_key
        price={'open':bar.open,'high':bar.high,'low':bar.low,'close':bar.close,'volume':bar.volume,
               'hl2':(bar.high+bar.low)/2,'hlc3':(bar.high+bar.low+bar.close)/3,
               'ohlc4':(bar.open+bar.high+bar.low+bar.close)/4}[source]
        amount=max(0,bar.volume)
        pv+=price*amount
        volume+=amount
        values.append(pv/volume if volume else None)
    return values,previous[-50:]


def summary_rows(analysis):
    """Five source states, published by their own close; price is chart close."""
    price=analysis.candles[-1].close if analysis.candles else None
    cutoff=analysis.asof
    rows=[]
    sources=analysis.summary_sources
    for tf in ('H1','H4','D1','W1','MN1'):
        source=sources.get(tf)
        if source is None and tf==analysis.timeframe:source=analysis
        state=None
        if source and source.candles:
            boundary=min(cutoff,source.asof) if cutoff and source.asof else cutoff or source.asof
            index=(bisect_right(source.candles,boundary,key=lambda b:candle_close_time(b,tf))-1
                   if boundary else len(source.candles)-1)
            if index>=0:
                state=source.flows[index]
        side=state.side if state is not None else 0
        guard=state.guard if state is not None else None
        working=bool(guard and side and not state.guard_broken and state.lo is not None and state.hi is not None
                     and guard.top>=state.lo and guard.bottom<=state.hi)
        in_zone=working and price is not None and guard.bottom<=price<=guard.top
        rows.append({'timeframe':tf,'flow':side,'fvg':side if in_zone else 0,
                     'available':state is not None})
    return rows
