"""Qt-independent VWAP and current instrument summary for the pinned settings."""
from .market_data import MOSCOW
from .exchange_time import ExchangeClock
from .publication import chart_cutoff,published_state
from .fvg_display import working_zone


def period_key(dt,anchor,*,exchange: ExchangeClock | None=None):
    if anchor=='Session' and exchange is not None:
        return exchange.vwap_session_key(dt)
    dt=dt.astimezone(exchange.zone if exchange is not None else MOSCOW)
    if anchor=='Session':return dt.date()
    if anchor=='Week':return dt.isocalendar()[:2]
    if anchor=='Month':return dt.year,dt.month
    if anchor=='Quarter':return dt.year,(dt.month-1)//3
    if anchor=='Year':return dt.year
    if anchor=='Decade':return dt.year//10
    if anchor=='Century':return dt.year//100
    raise ValueError(anchor)


def vwap_series(candles,anchor='Session',source='hlc3',*,exchange: ExchangeClock | None=None):
    values=[]
    previous=[]
    key=None
    pv=volume=0.0
    old=None
    previous_close=None
    segment=None
    zone=exchange.zone if exchange is not None else MOSCOW
    for i,bar in enumerate(candles):
        # Session is venue/product-specific. Supported CME Globex index,
        # COMEX metals and NYMEX futures reset at 18:00 ET across civil
        # midnight and skip the 17:00–18:00 halt. US equity Session
        # remains 04:00–20:00 ET (pre/core/postmarket).
        # Week/Month/other anchors still use the exchange-local calendar.
        next_key=period_key(bar.time,anchor,exchange=exchange)
        if anchor=='Session' and next_key is None:
            values.append(None)
            continue
        reset=key!=next_key
        if i and anchor in ('Decade','Century'):
            current_year=bar.time.astimezone(zone).year
            prior_year=candles[i-1].time.astimezone(zone).year
            reset=current_year!=prior_year and current_year%(10 if anchor=='Decade' else 100)==0
        if i==0 or reset:
            if key is not None and previous_close is not None:
                old=previous_close
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
        point=pv/volume if volume else None
        values.append(point)
        previous_close=point
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
        # Use the same confirmed guard, formation cutoff and live episode
        # bounds as the plotted higher FVG and the screener.
        zone=working_zone(source,cutoff,cutoff) if source and source.candles else None
        in_zone=zone is not None and price is not None and zone.bottom<=price<=zone.top
        rows.append({'timeframe':tf,'flow':side,'fvg':zone.side if in_zone else 0,
                     'available':state is not None})
    return rows
