"""v2.11 flow levels and diagnostic from the same confirmed source snapshot."""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime

from .market_data import MOSCOW,candle_close_time
from .publication import chart_cutoff,published_state

RATIOS=(0.0,0.4,0.5,0.6,1.0)


@dataclass(frozen=True)
class FlowLevel:
    start:datetime
    end:datetime
    price:float
    ratio:float
    previous:bool=False


@dataclass(frozen=True)
class FlowDisplay:
    state:object
    source_tf:str
    levels:tuple
    diagnostic:str


def _time(source,index,close=False):
    if index is None or not 0<=index<len(source.candles):return None
    bar=source.candles[index]
    return candle_close_time(bar,source.timeframe) if close else bar.time


def select_flow_display(analysis,parents,settings):
    lower=analysis.timeframe in ('M1','M5')
    source=parents.get('H1') if lower else analysis
    tf='H1' if lower else analysis.timeframe
    cutoff=chart_cutoff(analysis)
    state=index=None
    if source is not None:
        state,index,cutoff=published_state(source,cutoff,live=True)
    diagnostic=state.diagnosis if state is not None else 'Нет подтверждённого снимка потока'
    if state is not None:
        closed=candle_close_time(source.candles[index],tf)
        diagnostic+='\nСнимок на закрытие: '+closed.astimezone(MOSCOW).strftime('%Y-%m-%d %H:%M')
        current=bisect_right(source.candles,cutoff,key=lambda bar:bar.time)-1
        if index<current<len(source.flows):
            developing=source.flows[current]
            side=developing.pending_side
            if (side and side!=state.side and cutoff<candle_close_time(source.candles[current],tf)):
                diagnostic+='\nНа формирующейся свече: '+('ЛОНГ' if side==1 else 'ШОРТ')+'; подтверждение после её закрытия'
    diagnostic='ПОСЛЕДНЯЯ СМЕНА / СТАРТ | '+tf+'\n'+diagnostic
    levels=[]
    if state is not None and settings.flowShowLevels and (not lower or settings.flowH1OnM5):
        if settings.flowShowPrevious and state.previous is not None:
            old_start,old_end,lo,hi,_=state.previous
            start=state.previous_start_at or _time(source,old_start)
            end=state.previous_end_at or _time(source,old_end,True)
            if start is not None and end is not None and end>start and lo is not None and hi is not None:
                levels.extend(FlowLevel(start,end,lo+(hi-lo)*ratio,ratio,True) for ratio in RATIOS)
        start=state.range_start_at or _time(source,state.start)
        if state.side and start is not None and state.lo is not None and state.hi is not None:
            end=candle_close_time(analysis.candles[-1],analysis.timeframe)
            if end>start:
                levels.extend(FlowLevel(start,end,state.lo+(state.hi-state.lo)*ratio,ratio) for ratio in RATIOS)
    return FlowDisplay(state,tf,tuple(levels),diagnostic)
