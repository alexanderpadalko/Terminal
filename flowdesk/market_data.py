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
import urllib.error
import time as time_module
from functools import lru_cache
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .cancellation import checkpoint, pause

from .moex_calendar import (CLOSED, EXTRA, REGULAR, SessionOverride,
                            stock_session_kind, stock_trading_date,
                            EXTRA_AUCTION, MORNING_AUCTION, EXTRA_HOLIDAYS_2026)

MOSCOW = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
TIMEFRAMES = ("M1", "M5", "H1", "H4", "D1", "W1", "MN1")
MOEX_INTERVALS = {"M1": 1, "M5": 1, "H1": 60, "H4": 60, "D1": 24, "W1": 7, "MN1": 31}
NATIVE_HIGHER = ("D1", "W1", "MN1")

# Request the available exchange archive, including the initial liquidity
# context. The actual first returned bar is reported; availability is not
# equivalent to completeness or to the TradingView session's first bar.
CANONICAL_H1_DAYS = 36 * 366
CANONICAL_H1_LIMIT = 200000


@lru_cache(maxsize=128)
def _security_tick(symbol,board,day):
    """Only successful official MINSTEP responses enter the daily cache."""
    market='futures/markets/forts' if board=='RFUD' else 'stock/markets/shares'
    if board=='RFUD':
        from .futures_data import iss_symbol
        symbol=iss_symbol(symbol)
    url=f'https://iss.moex.com/iss/engines/{market}/boards/{board}/securities/{symbol}.json?iss.meta=off&iss.only=securities&securities.columns=SECID,MINSTEP'
    checkpoint()
    with urllib.request.urlopen(url,timeout=5) as response:
        table=json.load(response).get('securities',{})
    checkpoint()
    for row in table.get('data',[]):
        value=dict(zip(table.get('columns',[]),row)).get('MINSTEP')
        if value is not None and not isinstance(value,bool) and math.isfinite(float(value)) and float(value)>0:
            return float(value)
    raise ValueError('MINSTEP unavailable')


def security_tick(symbol,board,day):
    """Network failure/empty metadata is retryable, not cached as a day-long miss."""
    try:
        return _security_tick(symbol,board,day)
    except (OSError,ValueError,KeyError,TypeError):
        return None


@dataclass(frozen=True, slots=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    scheduled_close: datetime | None = None
    # Exchange settlement is a reference close, not necessarily a traded
    # price inside the session range (notably on illiquid deferred futures).
    settlement_close: bool = False
    # Logical MOEX trading period. ``time`` remains the first available
    # physical source timestamp, so weekend drafts are visible before Monday.
    trading_date: date | None = None

    def __post_init__(self):
        if self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("Время свечи должно содержать часовой пояс")
        if self.scheduled_close is not None and (self.scheduled_close.tzinfo is None or
                self.scheduled_close.utcoffset() is None or
                self.scheduled_close.astimezone(UTC) <= self.time.astimezone(UTC)):
            raise ValueError("Неверное время закрытия свечи")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("OHLCV содержит NaN/Inf")
        if type(self.settlement_close) is not bool:
            raise ValueError('Invalid settlement close flag')
        if self.trading_date is not None and type(self.trading_date) is not date:
            raise ValueError('Invalid trading date')
        prices=(self.open,self.low,self.high) if self.settlement_close else (self.open,self.close,self.low,self.high)
        if self.high < max(prices) or self.low > min(prices):
            raise ValueError(f"Неверный OHLC: {self.time}")


def normalize(candles: list[Candle]) -> list[Candle]:
    """Chronological order, last value wins when timestamps repeat."""
    return sorted({x.time.astimezone(UTC): x for x in candles}.values(), key=lambda x: x.time.astimezone(UTC))


def serialize_candle(c: Candle) -> dict:
    return {"time": c.time.isoformat(), "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume,
            **({"scheduled_close": c.scheduled_close.isoformat()} if c.scheduled_close else {}),
            **({"settlement_close":True} if c.settlement_close else {}),
            **({"trading_date":c.trading_date.isoformat()} if c.trading_date else {})}


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
    if tf == "H4":
        return dt.replace(hour=dt.hour // 4 * 4, minute=0, second=0, microsecond=0)
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


def candle_close_time(candle: Candle, tf: str) -> datetime:
    """Explicit scheduled end, otherwise a conservative nominal interval end.

    This is a conservative calendar cutoff, not an exchange session-close
    timestamp. Do not infer completion from the presence of the next bar.
    """
    if candle.scheduled_close is not None:
        return candle.scheduled_close
    if tf == "H4":
        return candle.time + timedelta(hours=4)
    if tf in ("M1", "M5", "H1"):
        return candle.time + timedelta(minutes={"M1": 1, "M5": 5, "H1": 60}[tf])
    start = period_floor(candle.time.astimezone(MOSCOW), tf)
    if tf == "D1":
        return start + timedelta(days=1)
    if tf == "W1":
        return start + timedelta(days=7)
    if tf == "MN1":
        return start.replace(year=start.year + (start.month == 12),
                             month=start.month % 12 + 1, day=1)
    raise ValueError(f"Неизвестный ТФ: {tf}")


def aggregate_moex_h4(candles: list[Candle], *, auction_overrides: dict | None = None) -> list[Candle]:
    """Four-hour bars anchored at the TQBR session open, from cleaned H1.

    Intraday bars follow the session opening, not the D1 midnight label.
    Verified 2026 final source hours end at 23:50 on evening sessions, at
    19:00 on EXTRA sessions. Other years retain conservative nominal ends.
    Historical admission to evening trading remains unverified: where it is
    unknown, nominal later closes are conservative, never inferred from a
    missing next trade. No empty bars or intermediate prices are fabricated.
    Sources: tradingview.com/pine-script-docs/concepts/time/;
    moex.com/n29024; moex.com/n77059; moex.com/n103379.
    """
    days = {}
    for bar in normalize(candles):
        days.setdefault(bar.time.astimezone(MOSCOW).date(), []).append(bar)
    result = []
    for day, hours in days.items():
        window = opening_auction_window(day, overrides=auction_overrides)
        earliest = hours[0].time.astimezone(MOSCOW).replace(minute=0, second=0, microsecond=0)
        configured = datetime.combine(day, window[1], MOSCOW) if window else earliest
        anchor = min(configured, earliest)
        short_session = stock_session_kind(day, auction_overrides) == EXTRA or day < date(2020, 6, 22)
        end = datetime.combine(day, time(19), MOSCOW) if short_session else datetime.combine(day + timedelta(days=1), time.min, MOSCOW)
        # Observed old sessions can exceed a generic historical schedule.
        # Retain all prices and avoid assigning a close before the last hour.
        from .moex_clock import session_end,confirmation_time
        end = max(session_end(day,'TQBR',auction_overrides) or end,
                  confirmation_time(hours[-1],'H1','TQBR',auction_overrides))
        grouped = {}
        for bar in hours:
            local = bar.time.astimezone(MOSCOW)
            key = anchor + timedelta(hours=4 * int((local - anchor).total_seconds() // 14400))
            grouped.setdefault(key, []).append(bar)
        for key, chunk in grouped.items():
            result.append(Candle(key, chunk[0].open, max(c.high for c in chunk), min(c.low for c in chunk),
                                 chunk[-1].close, sum(c.volume for c in chunk), min(key + timedelta(hours=4), end)))
    return result


def aggregate(candles: list[Candle], tf: str, tz: ZoneInfo = MOSCOW,
              *, moex_trading_day: bool = False,
              auction_overrides: dict | None = None) -> list[Candle]:
    """Resample cleaned bars; EXTRA days belong to next REGULAR trade date.

    By default candle bars use calendar dates in Moscow, exactly as chart
    interval boundaries do. The settlement trading date is distinct: the
    optional moex_trading_day=True remains for settlement-accounting tasks
    and must NOT be used for chart D/W/M or Pine parity. Intraday source
    timestamps are never shifted.
    """
    grouped: dict[datetime, list[Candle]] = {}
    for c in normalize(candles):
        local = c.time.astimezone(tz)
        if moex_trading_day and tf in ("D1", "W1", "MN1"):
            trade_day = stock_trading_date(local.date(), auction_overrides)
            if trade_day is None:
                raise ValueError(f"MOEX: свеча {local.isoformat()} в официально неторговый день. "
                                 "Проверьте календарь ~/.flowdesk/auction_windows.json")
            local = local.replace(year=trade_day.year, month=trade_day.month,
                                  day=trade_day.day)
        key = period_floor(local, tf)
        grouped.setdefault(key, []).append(c)
    return [Candle(key, cs[0].open, max(c.high for c in cs), min(c.low for c in cs),
                   cs[-1].close, sum(c.volume for c in cs),settlement_close=cs[-1].settlement_close)
            for key, cs in sorted(grouped.items())]


def load_session_overrides(path: str | Path | None = None) -> dict:
    """Load optional per-date MOEX calendar corrections.

    Legacy syntax: {"2026-12-12": ["08:50", "09:00"], "2026-12-13": null}
    Full syntax: {"2026-12-12": {"kind": "EXTRA", "auction":
                                  ["08:50", "09:00"],
                                  "trading_date": "2026-12-14"}}
    Session kind is REGULAR/EXTRA/CLOSED; `null` disables the auction only.
    """
    filename = Path(path) if path is not None else Path.home() / ".flowdesk" / "auction_windows.json"
    if not filename.is_file():
        return {}
    raw = json.loads(filename.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Календарь MOEX должен быть JSON-словарём дат")

    def parse_window(day: str, window):
        if window is None:
            return None
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError(f"Неверное время аукциона {day}")
        begins, ends = (time.fromisoformat(str(x)) for x in window)
        if begins >= ends:
            raise ValueError(f"Неверное время аукциона {day}")
        return begins, ends

    result = {}
    for daystr, setting in raw.items():
        day = date.fromisoformat(daystr)
        if isinstance(setting, dict):
            kind = setting.get("kind")
            if kind is not None and kind not in (REGULAR, EXTRA, CLOSED):
                raise ValueError(f"Неверный тип сессии {daystr}: {kind}")
            if "auction" not in setting:
                raise ValueError(f"Для {daystr} нужно указать 'auction' (пара времени или null)")
            window = parse_window(daystr, setting["auction"])
            dest = (date.fromisoformat(setting["trading_date"])
                    if setting.get("trading_date") else None)
            if kind == CLOSED and window is not None:
                raise ValueError(f"В закрытый день {daystr} аукцион невозможен")
            if dest is not None and dest < day:
                raise ValueError(f"Торговый день {dest} не может предшествовать сессии {day}")
            result[day] = SessionOverride(window, kind, dest)
        else:
            result[day] = parse_window(daystr, setting)
    return result


def opening_auction_window(day: date, board: str = "TQBR",
                           overrides: dict | None = None
                           ) -> tuple[time, time] | None:
    """Official TQBR session auction, including weekday public holidays.

    Calendar: MOEX 2024-2026 published notices; 2025-01-27 ordinary morning
    session restart. For extra holidays 2026 auction is 09:50-10:00 even on
    Monday/Wednesday/Friday. No OHLC/volume heuristics are used.
    """
    if board != "TQBR":
        raise ValueError("Для режима, отличного от TQBR, нет проверенного расписания аукционов")
    if overrides is not None and day in overrides:
        value = overrides[day]
        return value.auction if isinstance(value, SessionOverride) else value
    kind = stock_session_kind(day, overrides)
    if kind == CLOSED:
        return None
    if kind == EXTRA:
        return EXTRA_AUCTION
    return MORNING_AUCTION if day >= date(2025, 1, 27) else EXTRA_AUCTION


def exclude_opening_auction(candles: list[Candle], tf: str,
                            *, board: str = "TQBR",
                            overrides: dict[date, tuple[time, time] | None] | None = None
                            ) -> list[Candle]:
    """Remove auction source bars BEFORE any FVG, structure or flow analysis.

    M1: drop the auction's constituent minute candles. H1: remove the whole
    *auction-only* hour; permitted only when auction closes at the hour boundary.
    This does not filter finished higher candles. M5/H4 are rebuilt from
    filtered M1/H1; native D/W/M intentionally retain auction prices.
    Filtering a finished D1 cannot undo an auction price in its high/low.
    """
    if tf not in ("M1", "M5", "H1"):
        raise ValueError("Аукцион можно исключать только из исходных M1/M5/H1 до агрегации")
    if board.upper() == 'RFUD':
        from .futures_data import clean as clean_futures
        return clean_futures(candles, tf)
    clean = []
    for candle in candles:
        local = candle.time.astimezone(MOSCOW)
        if stock_session_kind(local.date(), overrides) == CLOSED:
            raise ValueError(f"MOEX вернула свечу {local.isoformat()} в неторговый день. "
                             "Уточните календарь через ~/.flowdesk/auction_windows.json")
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
                  timeout: int, board: str, require_full: bool = False) -> list[Candle]:
    now = datetime.now(MOSCOW)
    frm = (now - timedelta(days=days)).date().isoformat()
    to = now.date().isoformat()
    if board.upper() == 'RFUD':
        from .futures_data import iss_symbol
        symbol = iss_symbol(symbol)
        market_path = 'futures/markets/forts'
    else:
        market_path = 'stock/markets/shares'
    urlbase = f"https://iss.moex.com/iss/engines/{market_path}/boards/{board}/securities/{symbol}/candles.json"
    all_bars = deque(maxlen=limit)
    start = 0
    for _ in range(600):
        checkpoint()
        params = urllib.parse.urlencode({"interval": interval, "from": frm, "till": to,
                                         "start": start, "iss.meta": "off", "iss.only": "candles"})
        req = urllib.request.Request(urlbase + "?" + params,
                                     headers={"User-Agent": "FlowDesk/0.2.10 (confirmed source candles)",
                                              "Accept": "application/json"})
        for attempt in range(3):
            checkpoint()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    payload = json.load(response)
                checkpoint()
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise
                pause(attempt + 1)
        table = payload.get("candles", {})
        fields, chunk = table.get("columns", []), table.get("data", [])
        if not chunk:
            break
        for row in chunk:
            item = dict(zip(fields, row))
            if all(item.get(k) is not None for k in ("open", "high", "low", "close")):
                if require_full and len(all_bars) == limit:
                    raise ValueError("Архив MOEX превышает лимит; расчёт по обрезанной истории отменён")
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


def download_native_higher(symbol: str, tf: str, *, days: int = CANONICAL_H1_DAYS,
                           board: str = "TQBR", timeout: int = 15) -> list[Candle]:
    """Read exchange D/W/M OHLCV intact, including opening-auction prices.

    Only the chart label is normalized to the Moscow calendar period start
    (D1 00:00 by the user's decision). No prices or volumes are resampled.
    The full requested range is required; never substitute a truncated tail.
    """
    if tf not in NATIVE_HIGHER:
        raise ValueError("Готовые старшие свечи: только D1/W1/MN1")
    raw = _iss_download(symbol.strip().upper(), MOEX_INTERVALS[tf], days=days,
                        limit=CANONICAL_H1_LIMIT, timeout=timeout,
                        board=board.strip().upper(), require_full=True)
    return normalize([Candle(period_floor(c.time.astimezone(MOSCOW), tf),
                             c.open, c.high, c.low, c.close, c.volume) for c in raw])


def bundle_source_info(metadata: dict, tf: str) -> dict:
    """Separate native higher prices from auction-filtered intraday sources."""
    return dict(metadata.get("sources", {}).get(tf, metadata))


def download_moex(symbol: str, tf: str, days: int | None = None,
                  limit: int = 3000, timeout: int = 15, board: str = "TQBR",
                  *, canonical_h1: list[Candle] | None = None,
                  auction_overrides: dict[date, tuple[time, time] | None] | None = None
                  ) -> list[Candle]:
    """Read native D/W/M, clean M1/H1 and aggregate missing M5/H4 intervals.

    M1/M5 originate from MOEX minute candles; H1 from MOEX hour candles after
    dropping auction-only hours. H4 is rebuilt from cleaned H1. Native D/W/M
    retain exchange OHLCV, including auction prices, independently of H1.
    CSV imports are not modified: they are assumed already session-selected.
    """
    symbol, board = symbol.strip().upper(), board.strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{1,32}", symbol) or not re.fullmatch(r"[A-Z0-9_-]{2,24}", board):
        raise ValueError("Проверьте тикер и код режима торгов")
    if tf not in TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый ТФ: {tf}")
    if not 1 <= limit <= 100000:
        raise ValueError("Некорректный лимит свечей")
    if tf in NATIVE_HIGHER:
        return download_native_higher(symbol, tf, days=days if days is not None else CANONICAL_H1_DAYS,
                                      board=board, timeout=timeout)[-limit:]
    if auction_overrides is None:
        auction_overrides = load_session_overrides()
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
        if tf == 'H4' and board.upper() == 'RFUD':
            from .futures_data import aggregate_h4
            result = aggregate_h4(hourly)
        else:
            result = hourly if tf == "H1" else aggregate_moex_h4(hourly, auction_overrides=auction_overrides)
    if not result:
        raise ValueError("После исключения аукционов не осталось торговых свечей")
    return result[-limit:]


def load_moex_session_bundle(symbol: str, display_tf: str, *, board: str = "TQBR",
                             display_limit: int = 3000,
                             history_days: int = CANONICAL_H1_DAYS,
                             auction_overrides: dict[date, tuple[time, time] | None] | None = None,
                             higher_history_days: dict[str, int] | None = None
                             ) -> tuple[list[Candle], dict[str, list[Candle]], dict]:
    """Download cleaned H1 plus independent native D/W/M with intact OHLCV."""
    if board.upper() == 'RFUD':
        from .futures_data import bundle
        return bundle(symbol, display_tf, display_limit=display_limit, history_days=history_days,
                      higher_history_days=higher_history_days)
    if display_tf not in TIMEFRAMES:
        raise ValueError(f"Неподдерживаемый ТФ: {display_tf}")
    # Important: settlement dates must not shift chart D1/W1/MN1 bars.
    # Use the exchange archive for the calculation, not a three-year tail.
    if auction_overrides is None:
        auction_overrides = load_session_overrides()
    raw_hours = _iss_download(symbol.strip().upper(), 60, board=board.strip().upper(),
                              days=history_days, limit=CANONICAL_H1_LIMIT + history_days,
                              timeout=15, require_full=True)
    # Before the bundled calendar, an actual ISS print proves that a session
    # existed. Do not reject an observed historical working Saturday merely
    # because the fallback weekday calendar calls it closed. Retain the
    # existing pre-2025 auction window and identify this inference explicitly.
    observed_legacy_days = sorted({c.time.astimezone(MOSCOW).date() for c in raw_hours
                                  if c.time.astimezone(MOSCOW).year < 2024
                                  and c.time.astimezone(MOSCOW).date() not in auction_overrides
                                  and stock_session_kind(c.time.astimezone(MOSCOW).date()) == CLOSED})
    if observed_legacy_days:
        auction_overrides = dict(auction_overrides)
        for day in observed_legacy_days:
            auction_overrides[day] = SessionOverride(EXTRA_AUCTION, REGULAR, day)
    hours = exclude_opening_auction(raw_hours, "H1", board=board.strip().upper(),
                                     overrides=auction_overrides)
    if not hours:
        raise ValueError("После исключения аукционов не осталось истории H1")
    dropped_hours = len(raw_hours) - len(hours)
    families = {tf: download_native_higher(symbol, tf, board=board,
                                          days=(higher_history_days or {}).get(tf, CANONICAL_H1_DAYS))
                for tf in NATIVE_HIGHER}
    from .moex_clock import with_session_closes
    hours=with_session_closes(hours,'H1',board.strip().upper(),auction_overrides)
    families={tf:with_session_closes(bars,tf,board.strip().upper(),auction_overrides)
              for tf,bars in families.items()}
    families['H4'] = aggregate_moex_h4(hours, auction_overrides=auction_overrides)
    families["H1"] = hours
    if display_tf in ("M1", "M5"):
        visible = download_moex(symbol, display_tf, board=board,
                                limit=display_limit, auction_overrides=auction_overrides)
    else:
        visible = families[display_tf][-display_limit:]
    if not visible:
        raise ValueError("Нет очищенной истории для выбранного ТФ")
    overview = {
        "mode": "MOEX_NATIVE_HIGHER_WITH_CLEAN_INTRADAY",
        "higher_bar_grouping": "NATIVE_MOEX_OHLCV_MOSCOW_PERIOD_LABELS",
        "exchange_trading_date_usage": "SETTLEMENT_METADATA_ONLY_NOT_BAR_GROUPING",
        "base_h1_first": hours[0].time.isoformat(),
        "base_h1_last": hours[-1].time.isoformat(),
        "base_h1_bars": len(hours),
        "raw_h1_bars": len(raw_hours),
        "requested_history_days": history_days,
        "history_request": "AVAILABLE_EXCHANGE_ARCHIVE",
        "observed_legacy_trading_days": [d.isoformat() for d in observed_legacy_days],
        "legacy_session_schedule_verified": False,
        "auction_h1_removed": dropped_hours,
        "schedule_overrides": [d.isoformat() for d in sorted(auction_overrides)],
        "schedule_source": "Official MOEX 2024/2025/2026 stock-session notices; "
                           "2025-01-27 morning restart; per-date override supported",
        "calendar_years": [2024, 2025, 2026],
        "holiday_extra_sessions_removed": sorted({
            c.time.date().isoformat() for c in raw_hours
            if c.time.date() in EXTRA_HOLIDAYS_2026 and
            c not in hours
        }),
        "session_trade_date_mapping": [
            {"physical_date": d.isoformat(),
             "trade_date": stock_trading_date(d, auction_overrides).isoformat()}
            for d in sorted({b.time.astimezone(MOSCOW).date() for b in hours})
            if stock_session_kind(d, auction_overrides) == EXTRA and d.weekday() < 5
        ],
        "higher_from_clean_h1": False,
        "h4_grouping": "TQBR_SESSION_OPEN_ANCHORED_FROM_CLEAN_H1",
        "h4_history_session_admission_verified": False,
        "weekend_session": "EXTRA sessions stay on PHYSICAL date for D/W/M chart bars; "
                           "the next regular settlement trading day is diagnostic metadata",
        "warning": "2024-2026 MOEX official calendars bundled; earlier/later dates need "
                    "calendar overrides; pre-2024 trading days observed in ISS are retained "
                    "with the existing assumed auction window, not a verified historical schedule; "
                    "D/W/M use native MOEX OHLCV including auction prices; "
                    "2026 closing horizons use published market-session ends; instrument-specific "
                    "earlier closes and other years remain unverified and use conservative nominal ends",
        "confirmation_clock": "MOEX_2026_SESSION_CLOSE_WITH_NOMINAL_FALLBACK",
    }
    overview["sources"] = {
        tf: {"mode": "MOEX_NATIVE_CANDLES" if tf in NATIVE_HIGHER else "MOEX_CLEAN_INTRADAY",
             "symbol": symbol.strip().upper(), "board": board.strip().upper(),
             "native_higher": tf in NATIVE_HIGHER,
             "iss_interval": MOEX_INTERVALS[tf],
             "opening_auction_excluded": tf not in NATIVE_HIGHER,
             "higher_from_clean_h1": tf == "H4",
             "bar_label_policy": "MOSCOW_CALENDAR_PERIOD_START" if tf in NATIVE_HIGHER else "SOURCE_SESSION",
             "first": bars[0].time.isoformat(), "last": bars[-1].time.isoformat(), "bars": len(bars)}
        for tf, bars in families.items()
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
    if 14340 <= typical <= 14460:
        return "H4"
    if 82800 <= typical <= 90000:
        return "D1"
    if 5.5 * 86400 <= typical <= 8.5 * 86400:
        return "W1"
    if 27 * 86400 <= typical <= 33 * 86400:
        return "MN1"
    return None

