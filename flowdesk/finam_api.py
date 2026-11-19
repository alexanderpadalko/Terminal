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
from .price_format import tick_from_asset
from .finam_requests import RequestCoordinator

BASE_URL = "https://api.finam.ru"
CREDENTIAL_SERVICE = "FlowDesk.FinamTradeAPI"
CREDENTIAL_NAME = "read-only-secret"
FRAMES = {"M1": "TIME_FRAME_M1", "M5": "TIME_FRAME_M5",
          "H1": "TIME_FRAME_H1", "H4": "TIME_FRAME_H4",
          "D1": "TIME_FRAME_D", "W1": "TIME_FRAME_W", "MN1": "TIME_FRAME_MN"}
DEPTH_DAYS = {"M1": 7, "M5": 30, "H1": 30, "H4": 30,
              "D1": 365, "W1": 5 * 365, "MN1": 5 * 365}
SAFE_SYMBOL = re.compile(r"^[A-Z0-9._-]{1,32}@[A-Z0-9]{4}$")
QUOTE_LEVELS = {1:"LAST_PRICE",2:"BEST_BID_OFFER",3:"DEPTH_OF_MARKET",
                4:"DEPTH_OF_BOOK",5:"ACCESS_FORBIDDEN"}


def _quote_level(value):
    """Normalize Finam's protobuf JSON enum without exposing unknown text."""
    if type(value) is int:
        return QUOTE_LEVELS.get(value,"UNKNOWN")
    if isinstance(value,str):
        level=value.removeprefix("QUOTE_LEVEL_")
        if level in QUOTE_LEVELS.values():
            return level
    return "UNKNOWN"


class FinamAPIError(RuntimeError):
    def __init__(self, message, *, http_status=None, retry_after=None):
        super().__init__(message)
        self.http_status = http_status
        self.retry_after = retry_after


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
    if isinstance(value,bool):raise ValueError('Invalid Finam timestamp')
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
    if timeframe not in FRAMES:raise ValueError('Unknown Finam timeframe')
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
    values=tuple(decimal_number(row[name]) for name in ('open','high','low','close','volume'))
    if values[-1]<0:raise ValueError('Negative Finam volume')
    return Candle(ts,*values,scheduled_close=close)


class FinamAPI:
    """Thread-safe finite-life JWT obtained from local secret; never printed."""
    session_candles = True
    # Production candle policy: H4 is derived solely from Finam H1;
    # M5, H1, D1, W1, MN1 are the unchanged provider OHLCV sources.
    h1_only_h4_native_dwm = True
    native_higher_frames=('D1','W1','MN1')
    stream_frames=('M5','H1','D1','W1','MN1')
    def __init__(self, *, opener=None, secret_reader=None, clock=None, coordinator=None):
        self.opener = opener or urlopen
        self.secret_reader = secret_reader or read_secret
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._jwt = ""
        self._expiry = 0.0
        self.permissions = []
        self.permissions_status = "unknown"
        self._account_ids = []  # TokenDetails only; never in logs/UI/reports.
        self._exact_symbols = {}
        self._asset_ticks = {}
        self._asset_retry_after = {}  # throttle optional metadata errors
        self._asset_details = {}
        self.coordinator = coordinator or RequestCoordinator()

    def request_scope(self, *, priority=10, cancelled=lambda: False):
        return self.coordinator.scope(priority=priority, cancelled=cancelled)

    def invalidate_session(self):
        """Forget prior auth and account-scoped metadata after a secret change."""
        with self._lock:
            self._jwt=""
            self._expiry=0.0
            self.permissions=[]
            self.permissions_status="unknown"
            self._account_ids=[]
            self._exact_symbols.clear()
            self._asset_ticks.clear()
            self._asset_retry_after.clear()
            self._asset_details.clear()

    def exchange_symbol(self, normalized_symbol):
        """Preserve exact ticker casing in Finam API requests (e.g. SiZ6)."""
        with self._lock:
            return self._exact_symbols.get(normalized_symbol.upper(),normalized_symbol)

    def _json(self, method, path, payload=None, token=None):
        if method not in ("GET", "POST") or not path.startswith("/v1/"):
            raise ValueError("Only read-only Finam API endpoints are supported")
        if method == "POST" and path not in ("/v1/sessions", "/v1/sessions/details"):
            raise ValueError("Trade API writes are disabled")
        key = (method, path, token) if method == "GET" else None
        try:
            return self.coordinator.run(key, lambda: self._send_json(method, path, payload, token))
        except Exception as exc:
            # Preserve only a fixed operation code, never the path/query.
            if not getattr(exc,"diagnostic_operation",None):
                exc.diagnostic_operation=("auth" if path=="/v1/sessions" else
                    "token_details" if path=="/v1/sessions/details" else
                    "catalog" if path.startswith("/v1/assets/all?") else
                    "asset_details" if path.startswith("/v1/assets/") else
                    "candles" if "/bars?" in path else
                    "quotes" if "/quotes/latest" in path else "")
            raise

    def _send_json(self, method, path, payload=None, token=None):
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
            hints = {
                400: "Проверьте тикер, MIC и интервал запроса.",
                401: "Временный ключ недействителен или истёк.",
                403: "Недостаточно прав на запрошенные данные.",
                404: "Ресурс не найден: проверьте тикер, MIC и доступ токена.",
                429: "Превышен лимит запросов. Повторите позже.",
            }
            hint = hints.get(exc.code, "Сервис не выполнил запрос.")
            raw_retry = exc.headers.get("Retry-After", "") if exc.headers else ""
            retry_after = min(300, max(1, int(raw_retry))) if raw_retry.isdigit() else None
            raise FinamAPIError(f"Finam HTTP {exc.code}: {hint}",
                                http_status=exc.code, retry_after=retry_after) from None
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
                raw=details.get("md_permissions")
                self.permissions=[p for p in raw if isinstance(p,dict)] if isinstance(raw,list) else []
                self.permissions_status="verified" if isinstance(raw,list) else "unknown"
                account_ids=details.get("account_ids")
                self._account_ids=([account for account in account_ids
                                    if isinstance(account,str) and account]
                                   if isinstance(account_ids,list) else [])
            except FinamAPIError:
                self.permissions = []
                self.permissions_status = "unknown"
                self._account_ids = []
            return token

    def market_delay_minutes(self, mic):
        """Unknown rights use a conservative 15-minute observation horizon.

        Never treat an explicit ACCESS_FORBIDDEN permission as market-data
        entitlement. This fallback is a calculation policy, not proof that
        the provider is delaying its feed by exactly fifteen minutes.
        """
        values=[]
        for row in self.permissions:
            if not isinstance(row,dict) or (
                    row.get("mic")!=mic and not row.get("worldwide")):
                continue
            if _quote_level(row.get("quote_level")) in ("UNKNOWN","ACCESS_FORBIDDEN"):
                continue
            try:
                value=int(row.get("delay_minutes",15))
                if 0<=value<=1440:values.append(value)
            except (TypeError,ValueError,OverflowError):
                continue
        return min(values) if values else 15

    def permission_summary(self):
        """Allowlisted local-only rights; no account IDs or JWT."""
        summary=[]
        for p in self.permissions:
            mic=str(p.get("mic") or "*")
            level=_quote_level(p.get("quote_level"))
            if not re.fullmatch(r"[A-Z0-9]{4}|\*",mic):
                mic="?"
            try:
                delay=int(p.get("delay_minutes",15))
            except (ValueError,TypeError,OverflowError):
                delay=15
            summary.append({"mic":mic,"quote_level":level,
                            "delay_minutes":max(0,min(delay,1440))})
        return {"status":self.permissions_status,"permissions":summary}

    @staticmethod
    def _catalog_problem(message,page):
        error=FinamAPIError(message)
        error.diagnostic_kind="invalid_response"
        error.diagnostic_operation="catalog"
        error.diagnostic_page=page
        return error

    def _catalog_page(self,path,page):
        try:
            try:
                payload=self._json("GET",path,token=self.access_key())
            except FinamAPIError as exc:
                if exc.http_status!=401:raise
                payload=self._json("GET",path,token=self.access_key(force=True))
            if not isinstance(payload,dict) or not isinstance(payload.get("assets",[]),list):
                raise self._catalog_problem("Finam: некорректный справочник инструментов",page)
            return payload
        except Exception as exc:
            exc.diagnostic_page=page
            raise

    def active_assets(self, *, cancelled=lambda: False):
        """Read-only Finam instrument directory. No MOEX requests or trade API.

        Full active catalog is required for exact futures ticker/MIC matching.
        Read-only metadata GET is independent of the HTTP *candle history*
        permission; it never downloads prices or opens orders.
        """
        result=[]
        seen_symbols=set()
        visited=set()
        cursor=None
        for page in range(150):
            if cancelled():
                raise InterruptedError("Finam: получение справочника остановлено")
            params=urlencode({"only_active":"true",**({"cursor":cursor} if cursor else {})})
            path="/v1/assets/all?"+params
            payload=self._catalog_page(path,page+1)
            rows=payload.get("assets",[])
            for row in rows:
                if not isinstance(row,dict) or row.get("is_archived"):
                    continue
                key=row.get("symbol") or f"{row.get('ticker','')}@{row.get('mic','')}"
                if key not in seen_symbols:
                    seen_symbols.add(key)
                    result.append(row)
            next_cursor=payload.get("next_cursor")
            if not next_cursor or str(next_cursor)=="0":
                exact={}
                for row in result:
                    code=row.get("symbol") or (
                        str(row.get("ticker",""))+"@"+str(row.get("mic","")))
                    if "@" in code:
                        exact[code.upper()]=code
                with self._lock:
                    self._exact_symbols=exact
                return result
            if str(next_cursor) in visited:
                raise self._catalog_problem("Finam: справочник повторяет страницу, список не подтверждён",page+1)
            visited.add(str(next_cursor))
            cursor=str(next_cursor)
        raise self._catalog_problem("Finam: справочник не завершён, лимит 150 страниц",150)

    def _readonly_asset_get(self, path):
        try:
            return self._json("GET",path,token=self.access_key())
        except FinamAPIError as exc:
            if exc.http_status != 401:
                raise
            return self._json("GET",path,token=self.access_key(force=True))

    def clear_asset_details_cache(self):
        """Refresh expiry metadata after a user-triggered catalog recheck."""
        with self._lock:
            self._asset_details.clear()

    def asset_details(self,symbol):
        """Authoritative expiry/specification of ticker@MIC; GET only."""
        if not SAFE_SYMBOL.fullmatch(symbol.upper()):
            raise ValueError("Некорректный символ Finam")
        key=symbol.upper()
        with self._lock:
            if key in self._asset_details:
                return self._asset_details[key]
        path="/v1/assets/"+quote(self.exchange_symbol(symbol),safe="")
        try:
            detail=self._readonly_asset_get(path)
        except FinamAPIError as exc:
            if exc.http_status not in (400,404):
                raise
            with self._lock:
                ids=list(self._account_ids)
            if len(ids)!=1:
                raise  # Ambiguous or missing account: do not guess.
            # Finam's documented GetAsset query accepts account_id. Retry
            # once only after 400/404; no additional market-data requests.
            scoped=path+"?"+urlencode({"account_id":ids[0]})
            detail=self._readonly_asset_get(scoped)
        if not isinstance(detail,dict):
            raise FinamAPIError("Finam вернул неверную карточку инструмента")
        with self._lock:
            self._asset_details[key]=detail
        return detail

    def latest_quote(self,symbol):
        """Cumulative venue quote turnover/volume, never a trade request."""
        if not SAFE_SYMBOL.fullmatch(symbol):
            raise ValueError("Некорректный символ Finam")
        path="/v1/instruments/"+quote(self.exchange_symbol(symbol),safe="")+"/quotes/latest"
        answer=self._readonly_asset_get(path)
        if not isinstance(answer,dict):
            raise FinamAPIError("Finam вернул неверную последнюю котировку")
        return answer

    def asset_tick_size(self, symbol):
        """Read display precision from GET /v1/assets/{symbol}; no price-history HTTP.

        Uses Finam's min_step / 10**decimals and caches results per symbol.
        Missing or unavailable metadata falls back to the chart default.
        Network calls take place in a background FinamLoadWorker, not Qt GUI.
        """
        if not SAFE_SYMBOL.fullmatch(symbol):
            raise ValueError("Некорректный символ Finam")
        key = symbol.upper()
        with self._lock:
            if key in self._asset_ticks:
                return self._asset_ticks[key]
            if time.monotonic() < self._asset_retry_after.get(key,0):
                return None
        try:
            asset = self.asset_details(symbol)
        except FinamAPIError:
            # A metadata outage must not stall each incoming WebSocket bar.
            with self._lock:
                self._asset_retry_after[key] = time.monotonic()+120
            raise
        step = tick_from_asset(asset)
        tick = float(step) if step is not None else None
        with self._lock:
            self._asset_ticks[key] = tick
        return tick

    def cached_tick_size(self,symbol):
        """Read verified cached metadata without a network request."""
        with self._lock:return self._asset_ticks.get(symbol.upper())

    def bars(self, symbol, timeframe, *, now=None, start=None, end=None):
        """Fetch a bounded historical interval, within Finam's documented depth.

        start inclusive, end exclusive. Callers can walk earlier windows
        without silently mixing different futures contracts. The default
        remains the original single-range request for existing callers.
        """
        if not SAFE_SYMBOL.fullmatch(symbol) or timeframe not in FRAMES:
            raise ValueError("Некорректный символ или таймфрейм Finam")
        upper = end or now or self.clock()
        lower = start or (upper - timedelta(days=DEPTH_DAYS[timeframe]))
        if lower.tzinfo is None or upper.tzinfo is None or lower >= upper:
            raise ValueError("Некорректный временной интервал Finam")
        params = urlencode({"timeframe": FRAMES[timeframe],
                            "interval.start_time": lower.isoformat().replace("+00:00", "Z"),
                            "interval.end_time": upper.isoformat().replace("+00:00", "Z")})
        path = "/v1/instruments/" + quote(self.exchange_symbol(symbol), safe="") + "/bars?" + params
        try:
            result = self._json("GET", path, token=self.access_key())
        except FinamAPIError as exc:
            if exc.http_status != 401:
                raise
            result = self._json("GET", path, token=self.access_key(force=True))
        if not isinstance(result,dict) or not isinstance(result.get('bars'),list):
            raise ValueError('Invalid Finam bars response')
        if any(result.get(k) not in (None,'',0,'0',False) for k in ('next_cursor','next_token','has_more')):
            raise ValueError('Incomplete Finam bars response')
        bars=[]
        for row in result['bars']:
            if not isinstance(row,dict):raise ValueError('Invalid Finam bar')
            if row.get('symbol') and str(row['symbol']).upper()!=symbol.upper():
                raise ValueError('Wrong Finam candle symbol')
            if row.get('timeframe') and row['timeframe']!=FRAMES[timeframe]:
                raise ValueError('Wrong Finam candle timeframe')
            bar=finam_bar(row,timeframe)
            # Some native daily responses include an adjacent date despite
            # the documented half-open request. Exclude the whole outside
            # candle; never relabel it or leak its prices into the interval.
            if lower<=bar.time<upper:bars.append(bar)
        return normalize(bars)
