"""v2.11 higher-TF event reducer, with explicit confirmed source publication."""
from bisect import bisect_right
import re
from .liquidity import Capture,CaptureState,HIGHER_SOURCES,select_pine_capture
from .market_data import candle_close_time
from .reference_engine import stamp
from .reference_runtime import ReferenceContext
from .cancellation import checkpoint


def audit_captures(consumer,sources,*,asof,settings):
    frames=HIGHER_SOURCES[consumer.timeframe]
    contexts={tf:ReferenceContext(consumer.timeframe,settings) for tf in frames}
    ends={tf:[candle_close_time(b,tf) for b in src.candles] for tf,src in sources.items() if tf in frames}
    rows=[]
    converted_cache={}
    for i,bar in enumerate(consumer.candles):
        if i%256==0:checkpoint()
        closed=candle_close_time(bar,consumer.timeframe)
        if asof is not None and closed>asof:
            rows.append(CaptureState(bar.time,closed,{tf:'SOURCE_NOT_CLOSED' for tf in frames},None,0,
                                     consumer_tf=consumer.timeframe,confirmed=False))
            continue
        statuses,active,histories,new_events={}, {}, {}, []
        audit=[]
        for tf in frames:
            source=sources.get(tf)
            ctx=contexts[tf]
            ctx.tick=consumer.minimum_tick
            ctx.observe(consumer.candles,i)
            name={'MN1':'M','W1':'W','D1':'D'}.get(tf,tf)
            if source is None or tf not in ends:
                statuses[tf]='NO_DATA'
                audit.append(ctx.call('f_flowLiquidityEvents',None,name)[2])
                continue
            eligible=min(bar.time,source.asof) if source.asof is not None else bar.time
            j=bisect_right(ends[tf],eligible)-1
            level=None
            if j>=0:
                pair,state=source.pairs[j],source.flows[j]
                guard=state.guard
                working=bool(guard and state.side and not state.guard_broken and state.lo is not None
                             and guard.top>=state.lo and guard.bottom<=state.hi)
                last=min(closed,source.asof) if source.asof is not None else closed
                k=bisect_right(ends[tf],last)-1
                level=ctx._base['FlowLiquidity'].new(pair.high,pair.low,pair.high_key,pair.low_key,
                    guard.top if guard else None,guard.bottom if guard else None,state.side,working,
                    bool(state.guard_balanced),source.candles[j].close,int(ends[tf][j].timestamp()*1000),
                    source.candles[k].close if k>=0 else None,int(ends[tf][k].timestamp()*1000) if k>=0 else None,pair.direction)
            output=ctx.call('f_flowLiquidityEvents',level,name)
            audit.append(output[2])
            history=ctx.states['f_flowLiquidityEvents'].get('captureHistory',[])
            converted=[]
            for event in history:
                kind='FVG' if event.fromFvg else ('MIN/IDM' if event.side==1 else 'MAX/IDM')
                match=re.search(r'(?:MIN|MAX|IDM) ([0-9.+-]+)',event.detail)
                value=(event.top if event.side==1 else event.bottom) if event.fromFvg else float(match[1]) if match else 0.0
                key=(tf,event.side,event.capturedAt,event.detail,event.fromFvg,event.top,event.bottom)
                if key not in converted_cache:
                    converted_cache[key]=Capture(tf,event.side,stamp(event.capturedAt),kind,value,event.fromFvg,event.top,event.bottom,event.detail)
                converted.append(converted_cache[key])
            histories[tf]=tuple(converted)
            active[tf]=converted[-1] if converted else None
            statuses[tf]='READY' if level is not None else 'AWAIT_HIGHER_CLOSE'
            if active[tf] and active[tf].captured_at==bar.time:new_events.append(active[tf])
        picked=select_pine_capture(active)
        audit_text=''.join(audit)
        if rows and rows[-1].audit_text==audit_text:audit_text=rows[-1].audit_text
        rows.append(CaptureState(bar.time,closed,statuses,picked,sum(v=='READY' for v in statuses.values()),
                                 new_events=tuple(new_events),source_active=active,source_histories=histories,
                                 consumer_tf=consumer.timeframe,selection_reason='PINNED_V211',audit_text=audit_text))
    return rows
