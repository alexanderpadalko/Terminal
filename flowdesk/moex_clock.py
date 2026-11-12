"""Explicit MOEX 2026 market close horizon; never changes candle labels/OHLCV.

Official schedule: moex.com/n103379, moex.com/n95564, moex.com/n96571.
The bundled calendar covers holidays/closed weekends. Instrument admission
to earlier sessions is not inferred from an absence of trades. Unknown years,
boards, or prints on CLOSED days keep the conservative nominal cutoff.
"""
from dataclasses import replace
from datetime import datetime,time,timedelta

from .market_data import MOSCOW,candle_close_time,period_floor
from .moex_calendar import CLOSED,EXTRA,stock_session_kind


def session_end(day,board,sessions=None):
    if day.year!=2026 or board not in ('TQBR','RFUD'):
        return None
    kind=stock_session_kind(day,sessions)
    if kind==CLOSED:
        return None
    return datetime.combine(day,time(19) if kind==EXTRA else time(23,50),MOSCOW)


def with_session_closes(candles,tf,board,sessions=None):
    result=[]
    for bar in candles:
        if bar.scheduled_close is not None:
            result.append(bar)
            continue
        nominal=candle_close_time(bar,tf)
        if tf in ('D1','W1','MN1'):
            start=period_floor(bar.time.astimezone(MOSCOW),tf).date()
            day=(nominal-timedelta(microseconds=1)).astimezone(MOSCOW).date()
            end=None
            if start.year==day.year==2026:
                while day>=start:
                    end=session_end(day,board,sessions)
                    if end is not None:
                        break
                    day-=timedelta(days=1)
        else:
            end=session_end(bar.time.astimezone(MOSCOW).date(),board,sessions)
        if end is not None and bar.time<end<nominal:
            bar=replace(bar,scheduled_close=end)
        result.append(bar)
    return result


def confirmation_time(bar,tf,board,sessions=None):
    return candle_close_time(with_session_closes([bar],tf,board,sessions)[0],tf)
