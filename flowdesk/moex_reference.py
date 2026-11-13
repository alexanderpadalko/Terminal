"""MOEX ISS is used ONLY for the watchlist and Russian futures turnover.

Market candles, orders, quotes shown on charts and streaming still come
exclusively from Finam. This small read-only helper uses public MOEX daily
history for top-20 shares and RFUD volume/turnover to decide future rolls.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .watchlist import WatchlistService, get_json, pages
from .finam_universe import cached_top20


MOSCOW=ZoneInfo("Europe/Moscow")


def refresh_top20(*,watchlist=None,cancelled=lambda:False):
    """Rebuild 20-session exact VALUE ranking at startup; fall back if offline."""
    service=watchlist or WatchlistService()
    previous=service.cached()
    if previous is None or service.due(previous):
        try:
            value=service.refresh(cancelled=cancelled,stocks_only=True)
        except InterruptedError:
            raise
        except Exception as exc:
            rows,status=cached_top20(watchlist=service)
            return rows,status+" · MOEX недоступна; сохранён прежний состав"
    else:
        value=previous
    rows,status=cached_top20(watchlist=service)
    if len(rows)!=20:
        raise ValueError("MOEX: рейтинга ровно из 20 акций нет")
    return rows,status+" · MOEX: 20 завершённых торговых сессий"


class MoexFuturesVolume:
    """Read all RFUD futures once, then use precisely the exchange values.

    No synthetic price*volume turnover, no unverified cross-session values.
    Missing timestamp or a single field makes that contract ineligible for
    an automatic switch.
    """
    def __init__(self, *, fetch=get_json):
        self.fetch=fetch
        self.rows={}

    def refresh(self, *, cancelled=lambda:False):
        rows=pages("engines/futures/markets/forts/boards/RFUD/securities.json",
                   "marketdata",{},fetch=self.fetch,cancelled=cancelled)
        latest={}
        for row in rows:
            symbol=str(row.get("SECID") or "").upper()
            if not symbol:
                continue
            if symbol in latest:
                raise ValueError("MOEX RFUD: дублирующиеся серии фьючерса")
            latest[symbol]=row
        if not latest:
            raise ValueError("MOEX RFUD: обороты недоступны")
        self.rows=latest
        return len(latest)

    def quote(self, symbol):
        ticker=str(symbol).split("@",1)[0].upper()
        row=self.rows.get(ticker)
        if not row:
            raise KeyError("Фьючерс отсутствует в котировках MOEX RFUD")
        day=row.get("TRADEDATE")
        clock=row.get("UPDATETIME") or row.get("TIME")
        if not isinstance(day,str) or not isinstance(clock,str):
            raise ValueError("MOEX не прислала дату или время статистики")
        stamp=datetime.fromisoformat(day+"T"+clock).replace(tzinfo=MOSCOW)
        return {"quote":{
            "timestamp":stamp.isoformat(),
            "volume":{"value":str(row["VOLTODAY"])},
            "turnover":{"value":str(row["VALTODAY"])},
        }}
