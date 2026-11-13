"""Exchange-local chart clocks and regular-session boundaries.

UTC timestamps remain unchanged in Candle/SQLite/Pine. Only chart display
and session VWAP grouping use these clocks. Unmapped MICs are shown as UTC:
guessing an exchange's timezone would silently corrupt the chart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import re
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
    "RTSX": "Europe/Moscow",  # Russian derivatives venue
    "XCME": "America/Chicago",  # CME Globex
    "XCEC": "America/New_York",  # COMEX metals
    "XNYM": "America/New_York",  # NYMEX energy/metals
    "IFEU": "Europe/London",    # ICE Futures Europe (Brent)
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
    cme_index_future: bool = False

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

    def vwap_session_key(self, instant: datetime):
        """Exchange trading-date for session VWAP; None means no trading.

        NQ/ES CME Globex: Sunday 18:00 ET -> Friday 17:00 ET,
        daily maintenance 17:00–18:00 ET. Session belongs to the
        NEXT local trading date from 18:00 onward, continuing through
        midnight until 17:00 the following day. Use America/New_York
        solely for this boundary; the chart X-axis remains CME/Chicago.
        Holiday/early-close overrides require separate verified data.
        """
        if self.cme_index_future:
            if instant.tzinfo is None:
                raise ValueError("Биржевая сессия требует timezone-aware UTC метки")
            eastern=instant.astimezone(ZoneInfo("America/New_York"))
            weekday=eastern.weekday()
            wall=eastern.time().replace(tzinfo=None)
            if (weekday==5 or
                (weekday==6 and wall<time(18,0)) or
                (weekday==4 and wall>=time(17,0)) or
                time(17,0)<=wall<time(18,0)):
                return None
            return (eastern+timedelta(days=1)).date() if wall>=time(18,0) else eastern.date()
        local=self.local(instant)
        if self.us_regular_equity:
            # One extended equities session: premarket + core + postmarket.
            if not (local.weekday()<5 and
                    time(4,0)<=local.time().replace(tzinfo=None)<time(20,0)):
                return None
        # Other instruments keep their previous exchange-local calendar grouping.
        return local.date()

    def within_vwap_session(self, instant: datetime) -> bool:
        """Whether this candle contributes to the selected Session VWAP."""
        return self.vwap_session_key(instant) is not None


def exchange_clock(mic: str, symbol: str | None = None) -> ExchangeClock:
    """Use product-specific calendars only for verified CME NQ/ES series.

    Other CME futures (e.g. metals, energies) are deliberately unchanged.
    """
    mic = (mic or "").strip().upper()
    zone_name = _EXCHANGE_ZONES.get(mic)
    if zone_name is None:
        return ExchangeClock(mic, ZoneInfo("UTC"), known=False)
    ticker=(symbol or "").strip().upper().split("@",1)[0]
    cme_index=bool(mic=="XCME" and
                   re.fullmatch(r"(?:NQ|ES)[FGHJKMNQUVXZ][0-9]{1,4}",ticker))
    return ExchangeClock(mic, ZoneInfo(zone_name),
                         us_regular_equity=mic in _US_EQUITY_MICS,
                         cme_index_future=cme_index)


# Keep the old Moscow-only chart default for archived MOEX UI regression tests.
LEGACY_MOSCOW_CLOCK = exchange_clock("MISX")
