"""Finam-only curated universe; screenshot labels are NOT Finam API identifiers.

Do not guess a MIC or silently substitute a nearby futures contract.
The read-only Finam assets catalog is authoritative for actionable rows.
Russia top-10 uses the first ten rows of the last stored 20-session ranking, with stale
source dates displayed. This code makes no MOEX network request.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .watchlist import WatchlistService

US_FUTURES="us"
RU_STOCKS="stocks"
RU_FUTURES="ru"
CRYPTO="crypto"
STOCK_DISPLAY_LIMIT=10  # Ranked by turnover over 20 completed sessions.


@dataclass(frozen=True)
class Contract:
    display: str
    name: str
    group: str
    aliases: tuple[str,...]
    mics: tuple[str,...]


def _expires(root,month,year):
    return (root+month+str(year),root+month+str(year)[-2:],
            root+month+str(year)[-1])


def _us(display,name,root,month,year,mics):
    return Contract(display,name,US_FUTURES,_expires(root,month,year),mics)


def _ru(display,name,root,month='Z',year=2026):
    aliases=_expires(root,month,year)
    return Contract(display,name,RU_FUTURES,aliases,("RTSX",))


US_CONTRACTS=(
    _us("GCZ2026","Золото","GC","Z",2026,("XCEC","XCME")),
    _us("SIZ2026","Серебро","SI","Z",2026,("XCEC","XCME")),
    _us("NGZ2026","Природный газ","NG","Z",2026,("XNYM","XCME")),
    _us("BRNF2027","Brent","BRN","F",2027,("IFEU","XICE","XNYM")),
)
RU_CONTRACTS=(
    _ru("SIZ2026","Доллар / рубль","SI"),
    _ru("CRZ2026","Юань / рубль","CR"),
    _ru("EUZ2026","Евро / рубль","EU"),
    Contract("USDRUB.P","Вечный USD/RUB",RU_FUTURES,("USDRUBF","USDRUB.P"),("RTSX",)),
    Contract("CNYRUB.P","Вечный CNY/RUB",RU_FUTURES,("CNYRUBF","CNYRUB.P"),("RTSX",)),
    Contract("EURRUB.P","Вечный EUR/RUB",RU_FUTURES,("EURRUBF","EURRUB.P"),("RTSX",)),
    Contract("MXZ2026","Индекс Мосбиржи",RU_FUTURES,
             _expires("MX","Z",2026)+_expires("MIX","Z",2026),("RTSX",)),
)
assert len(US_CONTRACTS)==4 and len(RU_CONTRACTS)==7

GROUP_LABELS={
    CRYPTO:"КРИПТА",
    US_FUTURES:"ФЬЮЧЕРСЫ США · 4",
    RU_STOCKS:"АКЦИИ РФ · ТОП-10 ПО ОБОРОТУ",
    RU_FUTURES:"ФЬЮЧЕРСЫ РФ · 7",
}


@dataclass(frozen=True)
class Item:
    label: str
    name: str
    group: str
    symbol: str = ""
    verified: bool = False
    source: str = ""


def _canonical(text):
    return str(text or "").replace(" ","").upper()


def match_contract(contract, assets):
    """Only exact ticker variants on an exchange-specific MIC; no substitutions.

    An ambiguous mapping or missing contract is UNAVAILABLE, never the first
    ticker with the same prefix. Archived assets are excluded.
    """
    normalized_aliases={_canonical(x) for x in contract.aliases}
    results={}
    for row in assets:
        if not isinstance(row,dict) or row.get("is_archived"):
            continue
        ticker=_canonical(row.get("ticker"))
        mic=_canonical(row.get("mic"))
        if ticker not in normalized_aliases or mic not in contract.mics:
            continue
        if row.get("type") and "FUT" not in str(row["type"]).upper():
            continue
        symbol=_canonical(row.get("symbol")) or (ticker+"@"+mic)
        if symbol != ticker+"@"+mic:
            continue
        results[symbol]=row
    if len(results)!=1:
        return ""
    return next(iter(results))


def cached_top10(*, watchlist=None):
    """Top ten from exact 20-session turnover ranking; never HTTP/guesses.

    Returns a visible fallback with its source date when the archive is old.
    """
    service=watchlist or WatchlistService()
    value=service.cached()
    if not value:
        return [],"нет сохранённого рейтинга"
    stocks=[row for row in value["instruments"] if row.get("kind")=="stock"][:STOCK_DISPLAY_LIMIT]
    source=str(value.get("window",[])[-1] if value.get("window") else value.get("slot") or "неизвестно")
    suffix=" · сохранённый рейтинг"
    if not value.get("window"):
        suffix=" · исходный список (без подтверждённых оборотов)"
    result=[Item(r["symbol"],r.get("name") or r["symbol"],RU_STOCKS,
                 r["symbol"].upper()+"@MISX",True,source)
            for r in stocks]
    return result,source+suffix


def compose_universe(assets=(),*,watchlist=None):
    """Four US futures, up to ten ranked shares, seven Russian futures."""
    def contracts(items):
        rows=[]
        for c in items:
            resolved=match_contract(c,assets)
            rows.append(Item(c.display,c.name,c.group,resolved,bool(resolved)))
        return rows
    stocks,status=cached_top10(watchlist=watchlist)
    return {US_FUTURES:contracts(US_CONTRACTS),RU_STOCKS:stocks,
            RU_FUTURES:contracts(RU_CONTRACTS),
            CRYPTO:[Item(ticker+".P",name,CRYPTO,ticker+"@BINANCE",True,"Binance · USDT бессрочный")
                    for ticker,name in (("BTCUSDT","Bitcoin"),)]},status


def is_in_universe(symbol, universe):
    """Match normalized internal symbols without rewriting catalog/API casing."""
    normalized=symbol.upper()
    return any(row.symbol.upper()==normalized and row.verified
               for group in universe.values() for row in group)
