"""Exchange-local chart clocks and regular-session boundaries.

UTC timestamps remain unchanged in Candle/SQLite/Pine. Only chart display
and session VWAP grouping use these clocks. Unmapped MICs are shown as UTC:
guessing an exchange's timezone would silently corrupt the chart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


# Finam uses real venue MICs (Nasdaq AAPL, MSFT, TSLA, etc. on XNGS).
# XNAS remains a timezone alias for already saved preferences, not a valid
# Finam instrument lookup alias.
_US_EQUITY_MICS = frozenset((
    "XNGS", "XNMS", "XNCM", "XNAS", "XNYS", "ARCX", "XASE",
    "BATS", "BATY", "EDGA", "EDGX", "XCHI", "IEXG", "MEMX",
))
_EXCHANGE_ZONES = {
    "MISX": "Europe/Moscow",
    "MOEX": "Europe/Moscow",
    "XMOE": "Europe/Moscow",
    "XLON": "Europe/London",
    "XETR": "Europe/Berlin",
    "XFRA": "Europe/Berlin",
    "XPAR": "Europe/Paris",
    "XAMS": "Europe/Amsterdam",
    "XSWX": "Europe/Zurich",
    "XMIL": "Europe/Rome",
    "XMAD": "Europe/Madrid",
    "XTSE": "America/Toronto",
    "XTSX": "America/Toronto",
    "XTKS": "Asia/Tokyo",
    "XHKG": "Asia/Hong_Kong",
    "XSHG": "Asia/Shanghai",
    "XSHE": "Asia/Shanghai",
    "XSES": "Asia/Singapore",
    "XASX": "Australia/Sydney",
}
_EXCHANGE_ZONES.update({mic: "America/New_York" for mic in _US_EQUITY_MICS})


@dataclass(frozen=True, slots=True)
class ExchangeClock:
    mic: str
    zone: ZoneInfo
    us_regular_equity: bool = False
    known: bool = True

    def local(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError("Биржевое время требует исходной UTC-aware метки")
        return instant.astimezone(self.zone)

    def abbreviation(self, instant: datetime) -> str:
        value = self.local(instant)
        return "МСК" if self.zone.key == "Europe/Moscow" else value.tzname() or "UTC"

    def format(self, instant: datetime, *, seconds=False) -> str:
        local = self.local(instant)
        spec = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
        return f"{local.strftime(spec)} {self.abbreviation(instant)}"

    def within_vwap_session(self, instant: datetime) -> bool:
        """US day session VWAP: 04:00–20:00 ET, including pre/postmarket.

        The entire extended US equity day (premarket 04:00–09:30,
        regular 09:30–16:00, postmarket 16:00–20:00) forms ONE VWAP.
        Do not reset at 09:30, 16:00, UTC midnight or Moscow midnight.
        Overnight 21:00–04:00 is a *different* potential session and is
        excluded until Finam session metadata and user policy are verified.

        For other MICs do not guess their trading-day opening hours.
        They retain exchange-local calendar grouping of received bars.
        """
        if not self.us_regular_equity:
            return True
        dt = self.local(instant)
        return dt.weekday() < 5 and time(4, 0) <= dt.time().replace(tzinfo=None) < time(20, 0)


def exchange_clock(mic: str) -> ExchangeClock:
    mic = (mic or "").strip().upper()
    zone_name = _EXCHANGE_ZONES.get(mic)
    if zone_name is None:
        return ExchangeClock(mic, ZoneInfo("UTC"), known=False)
    return ExchangeClock(mic, ZoneInfo(zone_name), us_regular_equity=mic in _US_EQUITY_MICS)


# Keep the old Moscow-only chart default for archived MOEX UI regression tests.
LEGACY_MOSCOW_CLOCK = exchange_clock("MISX")
