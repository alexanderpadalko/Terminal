"""Resume only a changing, unconfirmed suffix from a frozen closed prefix.

The numeric engines still execute their original steps. Any correction of a
closed candle, closing boundary, settings/context change or backwards clock
falls back to their full replay. No history or diagnostics are shortened.
"""
from copy import deepcopy
from dataclasses import dataclass
from bisect import bisect_right

from .market_data import candle_close_time
from .liquidity import HIGHER_SOURCES


@dataclass(slots=True)
class ReplayCheckpoint:
    key: tuple
    candles: tuple
    observed: object
    engines: tuple
    pairs: list


def context_key(tf,settings,minimum_tick,exclude_gaps,higher,edge):
    # Include every closed source value the prefix's liquidity reducer could
    # consume, including captures cancelled at the final consumer close.
    sources=[]
    for frame in HIGHER_SOURCES.get(tf,()):
        source=(higher or {}).get(frame)
        if source is None:
            sources.append((frame,None));continue
        cutoff=min(edge,source.asof) if source.asof is not None else edge
        count=bisect_right(source.candles,cutoff,key=lambda b:candle_close_time(b,frame))
        sources.append((frame,tuple(source.candles[:count]),tuple(source.flows[:count]),tuple(source.pairs[:count])))
    return tf,settings.calculation_key(),minimum_tick,exclude_gaps,tuple(sources)


def clone_engines(engines):
    structure,fvg,pair,flow=engines
    # Published snapshots/candles are immutable during replay. Copy mutable
    # working state and list containers; share the large frozen prefix rows.
    memo={id(fvg._bars):list(fvg._bars),id(fvg._inside):list(fvg._inside)}
    if flow is not None:
        memo[id(flow.history)]=list(flow.history)
        if hasattr(flow,'times'):memo[id(flow.times)]=list(flow.times)
    return deepcopy(engines,memo)


def restore(previous,candles,tf,asof,settings,minimum_tick,exclude_gaps,higher):
    saved=getattr(previous,'replay_checkpoint',None)
    if saved is None or asof is None or saved.observed is None or asof<saved.observed:return None
    count=len(saved.candles)
    if not count or count>=len(candles):return None
    if bisect_right(candles,asof,key=lambda b:candle_close_time(b,tf))!=count:return None
    if tuple(candles[:count])!=saved.candles:return None
    edge=candle_close_time(candles[count-1],tf)
    if saved.key!=context_key(tf,settings,minimum_tick,exclude_gaps,higher,edge):return None
    return saved,clone_engines(saved.engines),list(saved.pairs)


def freeze(candles,count,tf,asof,settings,minimum_tick,exclude_gaps,higher,engines,pairs):
    if not count:return None
    edge=candle_close_time(candles[count-1],tf)
    return ReplayCheckpoint(context_key(tf,settings,minimum_tick,exclude_gaps,higher,edge),
                            tuple(candles[:count]),asof,clone_engines(engines),list(pairs))
