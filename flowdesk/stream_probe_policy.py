"""Diagnostic expectation only; never changes the market/indicator clock."""
from datetime import datetime, timedelta

from .market_data import MOSCOW, opening_auction_window
from .moex_clock import session_end


def minute_updates_expected(now, board):
    # DEMO minute candles arrive after the public feed delay. In particular,
    # after midnight the previous day's delayed tail can still be active.
    source = now.astimezone(MOSCOW)-timedelta(minutes=15)
    end = session_end(source.date(), board)
    if end is None:
        return True  # Unknown schedule: never convert missing data to success.
    if board == 'RFUD':
        from .futures_data import auction
        window = auction(source.date())
    else:
        window = opening_auction_window(source.date(), board)
    if window is None:
        return True
    start = datetime.combine(source.date(), window[1], MOSCOW)
    return start <= source < end
