"""Replay verified history blocks independently; keep chart indices global.

Pinned engine/math are unchanged. Missing source periods must not connect
structures or carry working FVGs into the next verified block. Historical
candles and their completed calculations remain available to pan/zoom.
"""
from bisect import bisect_left, bisect_right
from dataclasses import replace
from datetime import datetime

from .engine import analyze
from .market_data import candle_close_time
from .cancellation import checkpoint


def analyze_history(bars, tf, *, breaks=(), cached=None, **kwargs):
    boundaries=sorted({bisect_left(bars,datetime.fromisoformat(t),key=lambda b:b.time)
                       for t in breaks})
    boundaries=[i for i in boundaries if 0<i<len(bars)]
    if not boundaries:
        return analyze(bars,tf,cached=cached,**kwargs)
    results=[];blocks=[]
    previous={block.candles[0].time:(start,end,block)
              for start,end,block in getattr(cached,'history_blocks',()) if block.candles}
    for start,end in zip([0]+boundaries,boundaries+[len(bars)]):
        checkpoint()
        chunk=bars[start:end]
        saved=previous.get(chunk[0].time)
        old_start,old_end,old=saved if saved else (0,0,None)
        asof=kwargs.get('asof')
        reusable=(old is not None and old.candles==chunk and old.asof is not None and asof is not None
                  and old.asof<=asof and
                  bisect_right(chunk,old.asof,key=lambda b:candle_close_time(b,tf))==
                  bisect_right(chunk,asof,key=lambda b:candle_close_time(b,tf)) and
                  bisect_right(chunk,old.asof,key=lambda b:b.time)==bisect_right(chunk,asof,key=lambda b:b.time))
        if reusable:
            result=replace(old,asof=asof)
            # An exclusive block end is the artificial overlay boundary,
            # never a price-triggered break inside that block.
            result.fvg=[replace(z,broken=None) if z.broken==old_end else z for z in result.fvg]
            if result.confirmed_fvg is not None:
                result.confirmed_fvg=[replace(z,broken=None) if z.broken==old_end else z for z in result.confirmed_fvg]
            shift=start-old_start
        else:
            result=analyze(chunk,tf,cached=old,**kwargs)
            shift=start
        index=lambda i:i+shift if i is not None and i>=0 else i
        def zone(z):
            return replace(z,start=index(z.start),impulse=index(z.impulse),created=index(z.created),
                           broken=index(z.broken)) if z else None
        def pair(p):
            return replace(p,high_key=index(p.high_key),low_key=index(p.low_key)) if p else None
        if shift:
            result.segments=[replace(s,a=index(s.a),b=index(s.b)) for s in result.segments]
            result.fvg=[zone(z) for z in result.fvg]
            if result.confirmed_fvg is not None:
                result.confirmed_fvg=[zone(z) for z in result.confirmed_fvg]
            result.pairs=[pair(p) for p in result.pairs]
            result.flows=[replace(s,i=index(s.i),start=index(s.start),guard=zone(s.guard),
                                 liquidity=pair(s.liquidity),
                                 previous=((index(s.previous[0]),index(s.previous[1]),*s.previous[2:])
                                           if s.previous else None)) for s in result.flows]
            result.market={key:([index(i) for i in value] if key.endswith('StepBars') else
                               index(value) if key.endswith('Bar') or key=='source_right_index' else value)
                           for key,value in result.market.items()}
        if end<len(bars):
            # Expire old overlay zones exactly at the unavailable boundary.
            # Their historical states remain intact before that boundary.
            result.fvg=[replace(z,broken=end) if z.broken is None else z for z in result.fvg]
            if result.confirmed_fvg is not None:
                result.confirmed_fvg=[replace(z,broken=end) if z.broken is None else z
                                      for z in result.confirmed_fvg]
        results.append(result)
        blocks.append((start,end,result))
    checkpoint()
    last=results[-1]
    result=replace(last,candles=bars,
        segments=[s for r in results for s in r.segments],
        fvg=[z for r in results for z in r.fvg],
        flows=[s for r in results for s in r.flows],
        pairs=[p for r in results for p in r.pairs],
        liquidity_audit=[a for r in results for a in r.liquidity_audit],history_blocks=blocks)
    if last.confirmed_fvg is not None:
        result.confirmed_fvg=[z for r in results[:-1] for z in (r.confirmed_fvg or r.fvg)]+last.confirmed_fvg
    return result
