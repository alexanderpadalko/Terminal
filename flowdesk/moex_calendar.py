"""Pinned MOEX TQBR stock-session calendar for the historical comparison window.

Official Moscow Exchange notices (these later decisions supersede older 2025/26
holiday notices, which previously said the exchanges would be closed):
2026 holidays: https://www.moex.com/n96571
2026 weekends: https://www.moex.com/n95564
2025 weekends: https://www.moex.com/n77967
2025 holidays: https://www.moex.com/n73701
2024 holidays: https://www.moex.com/n64121

REGULAR = ordinary trading day; EXTRA = additional weekend/holiday session,
part of the next REGULAR exchange trading day; CLOSED = no TQBR trading.
The calendar concerns the equities market, not derivatives/FX. Special schedule
amendments after publication can be supplied through local date overrides.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

REGULAR = "REGULAR"
EXTRA = "EXTRA"
CLOSED = "CLOSED"


def _dates(year: int, *month_day: tuple[int, int]) -> frozenset[date]:
    return frozenset(date(year, m, d) for m, d in month_day)


def _ranges(year: int, *days: tuple[int, int, int]) -> frozenset[date]:
    return frozenset(date(year, month, day) for month, first, last in days for day in range(first, last + 1))


# Exceptional ADDITIONAL weekend-style sessions on ordinary weekdays.
EXTRA_HOLIDAYS_2026 = _dates(2026, (2, 23), (5, 1), (6, 12), (11, 4))
CLOSED_HOLIDAYS_2026 = _dates(2026, (1, 1), (1, 2), (1, 7), (3, 8), (5, 9), (12, 31))
CLOSED_WEEKENDS_2026 = _ranges(
    2026, (1, 3, 4), (1, 10, 11), (2, 14, 15), (3, 7, 8), (3, 21, 22),
    (5, 9, 10), (6, 20, 21), (8, 1, 2), (8, 15, 16), (9, 12, 13),
    (10, 24, 25), (11, 28, 29),
)

# Weekend trading in stocks launched 2025-03-01; before that all ordinary
# calendar weekends were CLOSED (except explicitly confirmed working Saturdays).
CLOSED_HOLIDAYS_2025 = _dates(
    2025, (1, 1), (1, 2), (1, 7), (2, 23), (3, 8), (5, 1), (5, 9),
    (6, 12), (11, 4), (12, 31),
)
CLOSED_WEEKENDS_2025 = _ranges(
    # Later amendments also excluded Aug 9/10 and Nov 22/23:
    # https://fs.moex.com/f/23333/dec-25-jan-26-rus-eng.pdf
    2025, (3, 8, 9), (3, 22, 23), (4, 19, 20), (5, 24, 25),
    (6, 21, 22), (8, 2, 3), (9, 20, 21), (10, 25, 26),
    (8, 9, 10), (11, 2, 2), (11, 22, 23), (12, 6, 7),
)
WORKING_SATURDAYS_2025 = _dates(2025, (11, 1))

CLOSED_HOLIDAYS_2024 = _dates(
    2024, (1, 1), (1, 2), (1, 7), (2, 23), (3, 8),
    (5, 1), (5, 9), (6, 12), (11, 4),
)
WORKING_SATURDAYS_2024 = _dates(2024, (4, 27), (11, 2), (12, 28))

# The historical hour bar may contain only auction prints from 09:50-10:00
# or 06:50-07:00. These are NOT low-volume / zero-range heuristics.
EXTRA_AUCTION = (time(9, 50), time(10, 0))
MORNING_AUCTION = (time(6, 50), time(7, 0))


@dataclass(frozen=True, slots=True)
class SessionOverride:
    """Optional full session correction; auction=None means explicitly no auction.

    ``kind`` may override the session day type. ``trading_date`` may override
    the EXTRA->next REGULAR assignment if the exchange publishes an exception.
    """
    auction: tuple[time, time] | None
    kind: str | None = None
    trading_date: date | None = None


def stock_session_kind(day: date, overrides: dict | None = None) -> str:
    override = (overrides or {}).get(day)
    if isinstance(override, SessionOverride) and override.kind is not None:
        return override.kind
    if day.year == 2026:
        if day in CLOSED_HOLIDAYS_2026 or day in CLOSED_WEEKENDS_2026:
            return CLOSED
        if day in EXTRA_HOLIDAYS_2026 or day.weekday() >= 5:
            return EXTRA
        return REGULAR
    if day.year == 2025:
        if day in CLOSED_HOLIDAYS_2025:
            return CLOSED
        if day in WORKING_SATURDAYS_2025:
            return REGULAR
        if day.weekday() >= 5:
            if day < date(2025, 3, 1) or day in CLOSED_WEEKENDS_2025:
                return CLOSED
            return EXTRA
        return REGULAR
    if day.year == 2024:
        if day in CLOSED_HOLIDAYS_2024:
            return CLOSED
        if day in WORKING_SATURDAYS_2024:
            return REGULAR
        return CLOSED if day.weekday() >= 5 else REGULAR
    # Before 2024 / after 2026: weekday baseline only, NOT an authoritative
    # exchange calendar. A special date can be supplied in overrides.
    return CLOSED if day.weekday() >= 5 else REGULAR


def stock_trading_date(day: date, overrides: dict | None = None) -> date | None:
    """Calendar date of the REGULAR trading day owning this session's trades.

    Unlike moving all weekends to Monday, this skips special holiday sessions
    until the nearest REGULAR day, even across week/month/year boundaries.
    """
    override = (overrides or {}).get(day)
    if isinstance(override, SessionOverride) and override.trading_date is not None:
        return override.trading_date
    kind = stock_session_kind(day, overrides)
    if kind == CLOSED:
        return None
    if kind == REGULAR:
        return day
    cursor = day
    for _ in range(15):
        cursor += timedelta(days=1)
        if stock_session_kind(cursor, overrides) == REGULAR:
            return cursor
    raise ValueError(f"После дополнительной сессии {day} не найден обычный торговый день")
