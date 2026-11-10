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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
TIMEFRAMES = ("M1", "M5", "H1", "D1", "W1", "MN1")
MOEX_INTERVALS = {"M1": 1, "M5": 1, "H1": 60, "D1": 24, "W1": 7, "MN1": 31}


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


def aggregate(candles: list[Candle], tf: str, tz: ZoneInfo = MOSCOW) -> list[Candle]:
    """Resample without filling missing minutes or crossing calendar periods."""
    grouped: dict[datetime, list[Candle]] = {}
    for c in normalize(candles):
        key = period_floor(c.time.astimezone(tz), tf)
        grouped.setdefault(key, []).append(c)
    return [Candle(key, cs[0].open, max(c.high for c in cs), min(c.low for c in cs), cs[-1].close, sum(c.volume for c in cs)) for key, cs in sorted(grouped.items())]


def without_session_zeros(candles: list[Candle], intraday: bool = True) -> list[Candle]:
    """Optional MOEX-only session boundary filter (not enabled without parity tests).

    A zero-range candle at a new calendar day is a candidate for exclusion,
    but a blanket exclusion would differ from some MOEX trading calendars.
    This helper is intentionally NOT applied to downloaded data by default.
    """
    if not intraday:
        return candles
    return [x for i, x in enumerate(candles) if not (i > 0 and x.time.date() != candles[i - 1].time.date() and x.high == x.low)]


def download_moex(symbol: str, tf: str, days: int | None = None, limit: int = 3000, timeout: int = 15, board: str = "TQBR") -> list[Candle]:
    """Read public MOEX ISS candles, paginating to the end (no API key required).

    M5 is derived from one-minute bars: ISS has no five-minute interval.
    Does not pretend the latest forming candle is final.
    """
    symbol = symbol.strip().upper()
    board = board.strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,32}", symbol) or not re.fullmatch(r"[A-Z0-9_-]{2,24}", board):
        raise ValueError("Проверьте тикер и код режима торгов")
    if tf not in MOEX_INTERVALS:
        raise ValueError(f"Неподдерживаемый ТФ: {tf}")
    if days is None:
        days = {"M1": 6, "M5": 14, "H1": 180, "D1": 1460, "W1": 3650, "MN1": 7300}[tf]
    if not 1 <= limit <= 10000:
        raise ValueError("Некорректный лимит свечей")
    now = datetime.now(MOSCOW)
    frm = (now - timedelta(days=days)).date().isoformat()
    to = now.date().isoformat()
    urlbase = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/{board}/securities/{symbol}/candles.json"
    all_bars = deque(maxlen=limit if tf != "M5" else limit * 5)
    # ISS page length varies; continue until an empty page instead of assuming
    # 500 records/page. Keep the newest candles rather than the oldest.
    start = 0
    for _ in range(250):
        params = urllib.parse.urlencode({"interval": MOEX_INTERVALS[tf], "from": frm, "till": to, "start": start, "iss.meta": "off", "iss.only": "candles"})
        request = urllib.request.Request(urlbase + "?" + params, headers={"User-Agent": "FlowDesk/0.1 (historical OHLCV viewer)", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        table = payload.get("candles", {})
        fields = table.get("columns", [])
        chunk = table.get("data", [])
        if not chunk:
            break
        for row in chunk:
            item = dict(zip(fields, row))
            dt = datetime.fromisoformat(item["begin"]).replace(tzinfo=MOSCOW)
            if all(item.get(k) is not None for k in ("open", "high", "low", "close")):
                all_bars.append(Candle(dt, float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]), float(item.get("volume") or 0)))
        start += len(chunk)
    if not all_bars:
        raise ValueError(f"MOEX не вернула свечи для {symbol} (режим {board}). Уточните тикер/площадку.")
    result = normalize(list(all_bars))
    if tf == "M5":
        result = aggregate(result, "M5")
    return result[-limit:]


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
