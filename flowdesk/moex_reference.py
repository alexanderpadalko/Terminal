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


class MoexFuturesDates:
    """Optional last-trading-day fallback for Russian contracts only.

    This is not an authoritative US futures calendar and never affects the
    market feed. When Finam GetAsset offers an expiration date, it wins.
    """
    def __init__(self, *, fetch=get_json):
        self.fetch=fetch
        self.dates={}

    def refresh(self, *, cancelled=lambda:False):
        from datetime import date
        rows=pages("engines/futures/markets/forts/boards/RFUD/securities.json",
                   "securities",
                   {"securities.columns":"SECID,BOARDID,LASTTRADEDATE"},
                   fetch=self.fetch,cancelled=cancelled)
        found={}
        for row in rows:
            if not isinstance(row,dict) or str(row.get("BOARDID","RFUD")).upper()!="RFUD":
                continue
            ticker=str(row.get("SECID") or "").upper()
            raw=row.get("LASTTRADEDATE")
            if not ticker or not isinstance(raw,str):
                continue
            try:
                last=date.fromisoformat(raw[:10])
            except ValueError:
                continue
            symbol=ticker+"@RTSX"
            if symbol in found and found[symbol]!=last:
                raise ValueError("MOEX: неоднозначная дата завершения торгов "+ticker)
            found[symbol]=last.isoformat()
        if not found:
            raise ValueError("MOEX: даты фьючерсов недоступны")
        self.dates=found
        return len(found)
