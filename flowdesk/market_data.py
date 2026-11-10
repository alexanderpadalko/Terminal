"""Portable, dependency-free OHLCV ingestion. Moscow Exchange ISS and TradingView CSV."""
from __future__ import annotations

import csv
from collections import deque
import io
import json
import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
TIMEFRAMES = ("M1", "M5", "H1", "D1", "W1", "MN1")
MOEX_INTERVALS = {"M1": 1, "M5": 1, "H1": 60, "D1": 24, "W1": 7, "MN1": 31}

# Safety limit for a single historical fetch. For full monthly parity, an
# extended cleaned history or a session-filtered TV CSV is still required.
CANONICAL_H1_DAYS = 1095  # roughly three calendar years
CANONICAL_H1_LIMIT = 30000


@dataclass(frozen=True, slots=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self):
        if self.time.tzinfo is None:
            raise ValueError("Время свечи должно содержать часовой пояс")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("OHLCV содержит NaN/Inf")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError(f"Неверный OHLC: {self.time}")


def normalize(candles: list[Candle]) -> list[Candle]:
    """Chronological order, last value wins when timestamps repeat."""
    return sorted({x.time.astimezone(UTC): x for x in candles}.values(), key=lambda x: x.time)


def parse_time(value: str) -> datetime:
    value = str(value).strip().strip('"')
    if re.fullmatch(r"\d{10}(?:\.\d+)?", value):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if re.fullmatch(r"\d{13}", value):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    value = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y.%m.%d %H:%M"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"Неизвестный формат времени: {value}")
    # TV exports Unix/ISO; naive CSV can be from MOEX, explicitly label Moscow.
    return dt if dt.tzinfo else dt.replace(tzinfo=MOSCOW)


def read_csv(path: str | Path) -> list[Candle]:
    """CSV exported from TradingView or generic OHLCV; supports BOM, ; and , separators."""
    content = Path(path).read_text(encoding="utf-8-sig")
    if not content.strip():
        raise ValueError("CSV пустой")
    sep = csv.Sniffer().sniff(content[:8192], delimiters=",;\t").delimiter
    reader = csv.DictReader(io.StringIO(content), delimiter=sep)
    cols = {k.strip().lower(): k for k in (reader.fieldnames or [])}
    def col(*names):
        for name in names:
            if name in cols:
                return cols[name]
        return None
    t_key = col("time", "date", "datetime", "timestamp", "дата", "время")
    o_key, h_key, l_key, c_key = (col("open", "открытие"), col("high", "максимум"), col("low", "минимум"), col("close", "закрытие"))
    v_key = col("volume", "vol", "объем", "объём")
    if not all((t_key, o_key, h_key, l_key, c_key)):
        raise ValueError("CSV должен содержать Time, Open, High, Low, Close (заголовки могут быть русскими)")
    result = []
    for row_idx, row in enumerate(reader, 2):
        try:
            if not row[t_key]:
                continue
            result.append(Candle(parse_time(row[t_key]), float(row[o_key]), float(row[h_key]), float(row[l_key]), float(row[c_key]), float(row[v_key]) if v_key and row[v_key] else 0.0))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Ошибка в CSV, строка {row_idx}: {exc}") from exc
    if not result:
        raise ValueError("CSV не содержит свечей")
    return normalize(result)


def period_floor(dt: datetime, tf: str) -> datetime:
    if tf == "M5":
        return dt.replace(minute=dt.minute // 5 * 5, second=0, microsecond=0)
    if tf == "H1":
        return dt.replace(minute=0, second=0, microsecond=0)
    if tf == "D1":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if tf == "W1":
        d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    if tf == "MN1":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if tf == "M1":
        return dt.replace(second=0, microsecond=0)
    raise ValueError(f"Неизвестный ТФ: {tf}")


def aggregate(candles: list[Candle], tf: str, tz: ZoneInfo = MOSCOW,
              *, moex_trading_day: bool = False) -> list[Candle]:
    """Resample cleaned bars without fabricating missing source candles.

    MOEX weekend stock sessions belong to the next trading day (normally
    Monday). For D/W/M only, assign those bars to the following Monday's
    settlement calendar bucket. Exact special-holiday calendars still require
    exchange-session verification; CSV imports retain their original dates.
    """
    grouped: dict[datetime, list[Candle]] = {}
    for c in normalize(candles):
        local = c.time.astimezone(tz)
        if moex_trading_day and tf in ("D1", "W1", "MN1") and local.weekday() >= 5:
            local = local + timedelta(days=7 - local.weekday())
        key = period_floor(local, tf)
        grouped.setdefault(key, []).append(c)
    return [Candle(key, cs[0].open, max(c.high for c in cs), min(c.low for c in cs),
                   cs[-1].close, sum(c.volume for c in cs))
            for key, cs in sorted(grouped.items())]


def load_session_overrides(path: str | Path | None = None) -> dict[date, tuple[time, time] | None]:
    """Optional per-date corrections from ~/.flowdesk/auction_windows.json.

    Example: {"2026-12-12": ["08:50", "09:00"], "2026-12-13": null}.
    This supports schedule changes without hardcoded weekday/weekend rules.
    """
    filename = Path(path) if path is not None else Path.home() / ".flowdesk" / "auction_windows.json"
    if not filename.is_file():
        return {}
    raw = json.loads(filename.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Календарь MOEX должен быть JSON-словарём дат")
    result = {}
    for daystr, window in raw.items():
        day = date.fromisoformat(daystr)
        if window is None:
            result[day] = None
        elif isinstance(window, list) and len(window) == 2:
            begins, ends = (time.fromisoformat(x) for x in window)
            if begins >= ends:
                raise ValueError(f"Неверное время аукциона {daystr}")
            result[day] = (begins, ends)
        else:
            raise ValueError(f"Неверная запись календаря MOEX: {daystr}")
    return result


def opening_auction_window(day: date, board: str = "TQBR",
                           overrides: dict[date, tuple[time, time] | None] | None = None
                           ) -> tuple[time, time] | None:
    """Opening-auction window for the standard TQBR morning-session profile.

    MOEX's current ordinary calendar: weekdays 06:50–07:00 Moscow; weekends
    with trading 09:50–10:00. A session can move on a particular date: callers
    may supply a date-keyed exchange-calendar override. None = no auction.
    Historical exceptional sessions require a separately verified override;
    never infer an auction from zero range/low volume alone.
    """
    if board != "TQBR":
        raise ValueError("Для режима, отличного от TQBR, нет проверенного расписания аукционов")
    if overrides is not None and day in overrides:
        return overrides[day]
    # Morning session for equities resumed only on 27 Jan 2025 (MOEX).
    # Before that, weekday opening auction was 09:50–10:00.
    morning = day.weekday() < 5 and day >= date(2025, 1, 27)
    return ((time(6, 50), time(7, 0)) if morning
            else (time(9, 50), time(10, 0)))


def exclude_opening_auction(candles: list[Candle], tf: str,
                            *, board: str = "TQBR",
                            overrides: dict[date, tuple[time, time] | None] | None = None
                            ) -> list[Candle]:
    """Remove auction source bars BEFORE any FVG, structure or flow analysis.

    M1: drop the auction's constituent minute candles. H1: remove the whole
    *auction-only* hour; permitted only when auction closes at the hour boundary.
    This deliberately does not filter finished M5/D1/W1/MN1 candles: they must
    be REBUILT from previously filtered M1/H1. Filtering a finished D1 candle
    cannot undo an auction price already embedded in its high/low.
    """
    if tf not in ("M1", "M5", "H1"):
        raise ValueError("Аукцион можно исключать только из исходных M1/M5/H1 до агрегации")
    clean = []
    for candle in candles:
        local = candle.time.astimezone(MOSCOW)
        window = opening_auction_window(local.date(), board, overrides)
        if window is None:
            clean.append(candle)
            continue
        opening, regular = window
        if tf in ("M1", "M5"):
            bar_start = local.time().replace(tzinfo=None)
            bar_end = (local + timedelta(minutes=1 if tf == "M1" else 5)).time().replace(tzinfo=None)
            if opening <= bar_start < regular:
                if bar_end > regular:
                    raise ValueError("Аукцион и торги смешаны в одной M5 свече: нужен M1")
            else:
                clean.append(candle)
        else:
            hour_start = local.replace(minute=0, second=0, microsecond=0)
            open_dt = datetime.combine(local.date(), opening, MOSCOW)
            regular_dt = datetime.combine(local.date(), regular, MOSCOW)
            if (hour_start <= open_dt < hour_start + timedelta(hours=1)
                    and not (regular_dt == hour_start + timedelta(hours=1))):
                # H1 mixes auction with trading. Never silently use this as
                # an auction-clean input (H1 OHLC cannot be decomposed).
                raise ValueError("Аукцион заканчивается внутри часа; нужен M1 для очистки H1")
            if not (hour_start <= open_dt < hour_start + timedelta(hours=1)
                    and regular_dt == hour_start + timedelta(hours=1)):
                clean.append(candle)
    return clean


def without_session_zeros(candles: list[Candle], intraday: bool = True) -> list[Candle]:
    """Legacy opt-in helper. Not used in session-clean MOEX ingestion.

    Auction classification depends on the trading phase, never only OHLC.
    """
    if not intraday:
        return candles
    return [x for i, x in enumerate(candles) if not (i > 0 and x.time.date() != candles[i - 1].time.date() and x.high == x.low)]


def _iss_download(symbol: str, interval: int, *, days: int, limit: int,
                  timeout: int, board: str) -> list[Candle]:
    now = datetime.now(MOSCOW)
    frm = (now - timedelta(days=days)).date().isoformat()
    to = now.date().isoformat()
    urlbase = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/{board}/securities/{symbol}/candles.json"
    all_bars = deque(maxlen=limit)
    start = 0
    for _ in range(600):
        params = urllib.parse.urlencode({"interval": interval, "from": frm, "till": to,
                                         "start": start, "iss.meta": "off", "iss.only": "candles"})
        req = urllib.request.Request(urlbase + "?" + params,
                                     headers={"User-Agent": "FlowDesk/0.2.6 (session-clean viewer)",
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.load(response)
        table = payload.get("candles", {})
        fields, chunk = table.get("columns", []), table.get("data", [])
        if not chunk:
            break
        for row in chunk:
            item = dict(zip(fields, row))
            if all(item.get(k) is not None for k in ("open", "high", "low", "close")):
                dt = datetime.fromisoformat(item["begin"]).replace(tzinfo=MOSCOW)
                all_bars.append(Candle(dt, float(item["open"]), float(item["high"]),
                                       float(item["low"]), float(item["close"]),
                                       float(item.get("volume") or 0)))
        start += len(chunk)
    else:
        raise RuntimeError("История MOEX слишком длинная для лимита запросов: отказ от неполного набора")
    if not all_bars:
        raise ValueError(f"MOEX не вернула свечи для {symbol} / {board}")
    return normalize(list(all_bars))


def download_moex(symbol: str, tf: str, days: int | None = None,
                  limit: int = 3000, timeout: int = 15, board: str = "TQBR",
                  *, canonical_h1: list[Candle] | None = None,
                  auction_overrides: dict[date, tuple[time, time] | None] | None = None
                  ) -> list[Candle]:
    """Return an opening-auction-clean series for EVERY supported timeframe.

    M1/M5 originate from MOEX minute candles; H1 from MOEX hour candles after
    dropping auction-only hours. D1/W1/MN1 are REBUILT from cleaned H1; never
    use MOEX native higher OHLC (they already include auction prices).

    High-timeframe history is limited by the available cleaned H1 lookback.
    CSV imports are not modified: they are assumed already session-selected.
    """
    symbol, board = symbol.strip().upper(), board.strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,32}", symbol) or not re.fullmatch(r"[A-Z0-9_-]{2,24}", board):
        raise ValueError("Проверьте тикер и код режима торгов")
    if tf not in TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый ТФ: {tf}")
    if not 1 <= limit <= 100000:
        raise ValueError("Некорректный лимит свечей")
    if tf in ("M1", "M5"):
        if days is None:
            days = 6 if tf == "M1" else 14
        minute_limit = limit if tf == "M1" else limit * 5
        minutes = _iss_download(symbol, 1, days=days, limit=minute_limit,
                                timeout=timeout, board=board)
        minutes = exclude_opening_auction(minutes, "M1", board=board, overrides=auction_overrides)
        result = minutes if tf == "M1" else aggregate(minutes, "M5")
    else:
        if canonical_h1 is not None:
            # Caller guarantees canonical_h1 was obtained with auction removal.
            hourly = normalize(canonical_h1)
        else:
            if days is None:
                days = 180 if tf == "H1" else CANONICAL_H1_DAYS
            h_limit = limit if tf == "H1" else CANONICAL_H1_LIMIT
            native = _iss_download(symbol, 60, days=days, limit=h_limit + days,
                                   timeout=timeout, board=board)
            hourly = exclude_opening_auction(native, "H1", board=board,
                                             overrides=auction_overrides)
        result = hourly if tf == "H1" else aggregate(hourly, tf, moex_trading_day=True)
    if not result:
        raise ValueError("После исключения аукционов не осталось торговых свечей")
    return result[-limit:]


def load_moex_session_bundle(symbol: str, display_tf: str, *, board: str = "TQBR",
                             display_limit: int = 3000,
                             history_days: int = CANONICAL_H1_DAYS,
                             auction_overrides: dict[date, tuple[time, time] | None] | None = None
                             ) -> tuple[list[Candle], dict[str, list[Candle]], dict]:
    """Download H1 ONCE and build all D/W/M parents from that cleaned source."""
    if display_tf not in TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый ТФ: {display_tf}")
    # Source window intentionally longer than the 3,000 bars displayed. MTF
    # signals near the left edge remain experimental until more history exists.
    if auction_overrides is None:
        auction_overrides = load_session_overrides()
    raw_hours = _iss_download(symbol.strip().upper(), 60, board=board.strip().upper(),
                              days=history_days, limit=CANONICAL_H1_LIMIT + history_days,
                              timeout=15)
    hours = exclude_opening_auction(raw_hours, "H1", board=board.strip().upper(),
                                     overrides=auction_overrides)
    if not hours:
        raise ValueError("После исключения аукционов не осталось истории H1")
    dropped_hours = len(raw_hours) - len(hours)
    families = {tf: aggregate(hours, tf, moex_trading_day=True) for tf in ("D1", "W1", "MN1")}
    families["H1"] = hours
    if display_tf in ("M1", "M5"):
        visible = download_moex(symbol, display_tf, board=board,
                                limit=display_limit, auction_overrides=auction_overrides)
    else:
        visible = families[display_tf][-display_limit:]
    if not visible:
        raise ValueError("Нет очищенной истории для выбранного ТФ")
    overview = {
        "mode": "MOEX_TQBR_OPENING_AUCTION_EXCLUDED_ALL_TFS",
        "base_h1_first": hours[0].time.isoformat(),
        "base_h1_last": hours[-1].time.isoformat(),
        "base_h1_bars": len(hours),
        "raw_h1_bars": len(raw_hours),
        "auction_h1_removed": dropped_hours,
        "schedule_overrides": [d.isoformat() for d in sorted(auction_overrides)],
        "schedule_source": "MOEX stock morning restart 2025-01-27; weekend opening 09:50; optional per-date calendar",
        "higher_from_clean_h1": True,
        "weekend_session": "assigned to next Monday trading date (MOEX standard)",
        "warning": "D/W/M ограничены длиной очищенной H1; время специальных сессий сверяется по календарю биржи",
    }
    return visible, families, overview


def infer_timeframe(candles: list[Candle]) -> str | None:
    """Infer TradingView CSV interval from common *small* positive gaps.

    Large session/weekend gaps are ignored by taking the lowest typical bucket.
    Never use inferred period to fabricate extra candles.
    """
    if len(candles) < 3:
        return None
    gaps = sorted((candles[i].time - candles[i - 1].time).total_seconds() for i in range(1, len(candles)) if candles[i].time > candles[i - 1].time)
    if not gaps:
        return None
    typical = gaps[max(0, min(len(gaps) - 1, len(gaps) // 5))]
    if 55 <= typical <= 65:
        return "M1"
    if 290 <= typical <= 310:
        return "M5"
    if 3540 <= typical <= 3660:
        return "H1"
    if 82800 <= typical <= 90000:
        return "D1"
    if 5.5 * 86400 <= typical <= 8.5 * 86400:
        return "W1"
    if 27 * 86400 <= typical <= 33 * 86400:
        return "MN1"
    return None
