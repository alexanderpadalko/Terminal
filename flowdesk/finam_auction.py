"""Exclude the *closing* auction of MOEX Russian stocks from intraday analysis.

The official equity T+1 schedule moved to 18:55–19:00 Moscow on 2026-03-23.
This filter applies only to exchange-local M1 and M5 stock bars before
calculating FVG/structure/flow. It never alters raw Finam bars in SQLite.
Native higher OHLC needs its own source history to remove auction trades;
do not silently rewrite a mixed H1/H4/D/W/M OHLC by guessing prices.
"""
from __future__ import annotations

from datetime import date, time
from .market_data import MOSCOW

_EFFECTIVE = date(2026, 3, 23)
_AUCTION_START = time(18, 55)
_AUCTION_END = time(19, 0)
_STOCK_MICS = frozenset(("MISX", "XMOE"))


def is_stock_closing_auction_bar(bar, timeframe, mic):
    if mic.upper() not in _STOCK_MICS or timeframe not in ("M1", "M5"):
        return False
    # A Finam bar timestamp is the opening of the candle in UTC, not the
    # provider's receipt time. Always convert with Moscow's zone.
    local = bar.time.astimezone(MOSCOW)
    wall = local.time().replace(tzinfo=None)
    return (local.date() >= _EFFECTIVE and local.weekday() < 5
            and _AUCTION_START <= wall < _AUCTION_END)


def exclude_stock_closing_auction(series, mic):
    """Return display/calculation sources with auction bars removed.

    Keys other than M1/M5 are passed through unchanged: their aggregated
    exchange OHLC cannot be purged without reliable sub-candle history.
    Cache the raw series so future corrected exchange data is not lost.
    """
    if mic.upper() not in _STOCK_MICS:
        return series
    return {
        tf: ([bar for bar in bars
              if not is_stock_closing_auction_bar(bar, tf, mic)]
             if tf in ("M1", "M5") else bars)
        for tf, bars in series.items()
    }
