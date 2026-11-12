"""Shared v2.11 right-edge cutoff and confirmed/live source selection."""
from bisect import bisect_right
from dataclasses import replace
from .market_data import candle_close_time


def chart_cutoff(analysis):
    if not analysis.candles:
        return analysis.asof
    end=candle_close_time(analysis.candles[-1],analysis.timeframe)
    return min(analysis.asof,end) if analysis.asof is not None else end


def source_cutoff(source,cutoff):
    return min(cutoff,source.asof) if source.asof is not None and cutoff is not None else cutoff


def published_state(source,cutoff,*,live=False):
    cutoff=source_cutoff(source,cutoff)
    if cutoff is None or not source.candles or not source.flows:
        return None,None,cutoff
    index=bisect_right(source.candles,cutoff,key=lambda bar:candle_close_time(bar,source.timeframe))-1
    if not 0<=index<len(source.flows):
        return None,None,cutoff
    state=source.flows[index]
    if live:
        current=bisect_right(source.candles,cutoff,key=lambda bar:bar.time)-1
        if index<current<len(source.flows):
            developing=source.flows[current]
            open_bar=source.candles[current]
            same=(developing.live_range and developing.side==state.side and developing.start==state.start)
            if (same and open_bar.time<=cutoff<candle_close_time(open_bar,source.timeframe)
                    and state.lo is not None and state.hi is not None
                    and developing.lo is not None and developing.hi is not None):
                state=replace(state,lo=min(state.lo,developing.lo) if state.side==-1 else state.lo,
                              hi=max(state.hi,developing.hi) if state.side==1 else state.hi)
    return state,index,cutoff
