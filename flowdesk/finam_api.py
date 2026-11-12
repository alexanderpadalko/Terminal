"""Finam Trade API read-only REST client and OS-backed credential access.

Only /v1/sessions, /v1/sessions/details and GET /v1/instruments/.../bars
are used. No order/account trading methods exist in this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import json
import os
import re
import threading
import time

from .market_data import Candle, normalize

BASE_URL = "https://api.finam.ru"
CREDENTIAL_SERVICE = "FlowDesk.FinamTradeAPI"
CREDENTIAL_NAME = "read-only-secret"
FRAMES = {"M1": "TIME_FRAME_M1", "M5": "TIME_FRAME_M5",
          "H1": "TIME_FRAME_H1", "H4": "TIME_FRAME_H4",
          "D1": "TIME_FRAME_D", "W1": "TIME_FRAME_W", "MN1": "TIME_FRAME_MN"}
DEPTH_DAYS = {"M1": 7, "M5": 30, "H1": 30, "H4": 30,
              "D1": 365, "W1": 5 * 365, "MN1": 5 * 365}
SAFE_SYMBOL = re.compile(r"^[A-Z0-9._-]{1,32}@[A-Z0-9]{4}$")


class FinamAPIError(RuntimeError):
    pass


def read_secret():
    """Never put a secret in SettingsStore, logs, diagnostics, or SQLite."""
    supplied = os.environ.get("FINAM_TRADE_API_SECRET", "").strip()
    if supplied:
        return supplied
    try:
        import keyring
        return keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_NAME) or ""
    except Exception:
        return ""


def save_secret(secret):
    if not secret or not secret.strip():
        raise ValueError("Введите секретный токен Финама")
    import keyring
    keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_NAME, secret.strip())


def delete_secret():
    import keyring
    try:
        keyring.delete_password(CREDENTIAL_SERVICE, CREDENTIAL_NAME)
    except keyring.errors.PasswordDeleteError:
        pass


def symbol_name(ticker, mic):
    symbol = f"{ticker.strip().upper()}@{mic.strip().upper()}"
    if not SAFE_SYMBOL.fullmatch(symbol):
        raise ValueError("Формат инструмента: тикер@MIC, например AAPL@XNAS")
    return symbol


def parse_instant(value):
    if isinstance(value, dict):
        return datetime.fromtimestamp(int(value["seconds"]) + int(value.get("nanos", 0)) / 1e9,
                                      tz=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Finam returned timestamp without timezone")
        return result.astimezone(timezone.utc)
    raise ValueError("Неизвестный формат времени свечи Finam")


def decimal_number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        raise ValueError("Отсутствует цена или объём Finam")
    return float(value)


def finam_bar(row, timeframe):
    ts = parse_instant(row["timestamp"])
    # Use exchange provided open; do not anchor US bars to MOEX calendars.
    if timeframe in ("M1", "M5", "H1", "H4"):
        close = ts + timedelta(minutes={"M1":1, "M5":5, "H1":60, "H4":240}[timeframe])
    elif timeframe == "D1":
        close = ts + timedelta(days=1)
    elif timeframe == "W1":
        close = ts + timedelta(days=7)
    else:
        month = ts.month + 1
        year = ts.year + (month > 12)
        close = ts.replace(year=year, month=1 if month > 12 else month, day=1)
    return Candle(ts, *(decimal_number(row[name]) for name in
                        ("open", "high", "low", "close", "volume")), scheduled_close=close)


class FinamAPI:
    """Thread-safe finite-life JWT obtained from local secret; never printed."""
    def __init__(self, *, opener=None, secret_reader=None, clock=None):
        self.opener = opener or urlopen
        self.secret_reader = secret_reader or read_secret
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._jwt = ""
        self._expiry = 0.0
        self.permissions = []

    def _json(self, method, path, payload=None, token=None):
        if method not in ("GET", "POST") or not path.startswith("/v1/"):
            raise ValueError("Only read-only Finam API endpoints are supported")
        if method == "POST" and path not in ("/v1/sessions", "/v1/sessions/details"):
            raise ValueError("Trade API writes are disabled")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "FlowDesk/0.2.26"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(BASE_URL + path, data=body, headers=headers, method=method)
        try:
            with self.opener(req, timeout=16) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Do not echo response/request bodies: they could contain secrets.
            raise FinamAPIError(f"Finam HTTP {exc.code}, {path.split('?')[0]}") from None
        except (TimeoutError, URLError, OSError) as exc:
            raise FinamAPIError(f"Нет связи с Finam Trade API ({type(exc).__name__})") from None

    def access_key(self, *, force=False):
        with self._lock:
            if not force and self._jwt and time.monotonic() < self._expiry:
                return self._jwt
            secret = self.secret_reader()
            if not secret:
                raise FinamAPIError("Токен Finam не настроен. Добавьте его в настройках.")
            payload = self._json("POST", "/v1/sessions", {"secret": secret})
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                raise FinamAPIError("Finam не прислал временный access key")
            self._jwt, self._expiry = token, time.monotonic() + 8 * 60
            # Failure of the metadata lookup does not invalidate valid credentials.
            try:
                details = self._json("POST", "/v1/sessions/details", {"token": token})
                self.permissions = details.get("md_permissions") or []
            except FinamAPIError:
                self.permissions = []
            return token

    def market_delay_minutes(self, mic):
        """Unknown rights use a conservative 15-minute observation horizon."""
        values = [int(p.get("delay_minutes", 15)) for p in self.permissions
                  if isinstance(p, dict) and (p.get("mic") == mic or p.get("worldwide"))]
        return max(0, min(values)) if values else 15

    def bars(self, symbol, timeframe, *, now=None):
        if not SAFE_SYMBOL.fullmatch(symbol) or timeframe not in FRAMES:
            raise ValueError("Некорректный символ или таймфрейм Finam")
        end = now or self.clock()
        start = end - timedelta(days=DEPTH_DAYS[timeframe])
        params = urlencode({"timeframe": FRAMES[timeframe],
                            "interval.start_time": start.isoformat().replace("+00:00", "Z"),
                            "interval.end_time": end.isoformat().replace("+00:00", "Z")})
        path = "/v1/instruments/" + quote(symbol, safe="") + "/bars?" + params
        try:
            result = self._json("GET", path, token=self.access_key())
        except FinamAPIError as exc:
            if "HTTP 401" not in str(exc):
                raise
            result = self._json("GET", path, token=self.access_key(force=True))
        return normalize([finam_bar(row, timeframe) for row in result.get("bars", [])])
