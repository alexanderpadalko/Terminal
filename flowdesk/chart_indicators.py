"""Qt-independent VWAP and current instrument summary for the pinned settings."""
from .market_data import MOSCOW
from .publication import chart_cutoff,published_state


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
    cutoff=chart_cutoff(analysis)
    rows=[]
    sources=analysis.summary_sources
    for tf in ('H1','H4','D1','W1','MN1'):
        source=analysis if tf==analysis.timeframe else sources.get(tf)
        state=None
        if source and source.candles:
            state,_,_=published_state(source,cutoff,live=tf=='H1')
        side=state.side if state is not None else 0
        guard=state.guard if state is not None else None
        working=bool(guard and side and not state.guard_broken and state.lo is not None and state.hi is not None
                     and guard.top>=state.lo and guard.bottom<=state.hi)
        in_zone=working and price is not None and guard.bottom<=price<=guard.top
        rows.append({'timeframe':tf,'flow':side,'fvg':side if in_zone else 0,
                     'available':state is not None})
    return rows
