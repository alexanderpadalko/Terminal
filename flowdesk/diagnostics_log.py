"""Private, rotating, allow-listed diagnostics for the read-only FlowDesk terminal.

No API response, exception message, URL, token, account number or candle payload
is ever written. Only known event codes and parsed non-secret metadata survive.
The file lives in the user's local profile and is never sent to GitHub.
"""
from __future__ import annotations

from datetime import date
from logging import Formatter, INFO, LogRecord
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import re
import threading
import time

LOG_PATH = Path.home() / ".flowdesk" / "logs" / "diagnostics.log"
_MAX_BYTES = 1_000_000
_BACKUPS = 3
_LOCK = threading.RLock()
_EVENTS = frozenset({
    "catalog_start", "catalog_complete", "catalog_unavailable",
    "stock_ranking_unavailable", "moex_expiry_unavailable",
    "catalog_worker_failed", "chart_load_failed",
    "stream_unavailable", "stream_reconnecting",
})
_SYMBOL = re.compile(r"^[A-Za-z0-9_.-]{1,24}@[A-Z0-9]{4}$")
_LABEL = re.compile(r"^[A-Z0-9.]{2,24}$")
_DAY = re.compile(r"(?:действует до |новая дата |экспирация )(\d{4}-\d{2}-\d{2})")
_HTTP = re.compile(r"\bHTTP\s+([1-5]\d{2})\b", re.IGNORECASE)


def _safe_symbol(value):
    text = str(value or "")
    return text if _SYMBOL.fullmatch(text) else "-"


def _http_code(value):
    match = _HTTP.search(str(value or ""))
    return "http_" + match.group(1) if match else "none"


def _write(fields, *, path=None):
    """A single serialized file write; logging failure never blocks a chart."""
    target = Path(path) if path is not None else LOG_PATH
    with _LOCK:
        handler = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(target.parent, 0o700)
            handler = RotatingFileHandler(target, maxBytes=_MAX_BYTES,
                                          backupCount=_BACKUPS, encoding="utf-8")
            if os.name != "nt":
                os.chmod(target, 0o600)
            formatter = Formatter("%(asctime)sZ %(levelname)s %(message)s",
                                  datefmt="%Y-%m-%dT%H:%M:%S")
            formatter.converter = time.gmtime
            handler.setFormatter(formatter)
            message = " ".join(key + "=" + value for key, value in fields)
            record = LogRecord("flowdesk.diagnostics", INFO, "", 0,
                               message, (), None)
            handler.emit(record)
            return True
        except (OSError, ValueError):
            return False
        finally:
            if handler is not None:
                handler.close()


def log_event(event, *, symbol="", error=None, path=None):
    """Record only a known event and an optional HTTP status."""
    if event not in _EVENTS:
        return False
    return _write((("event", event),
                   ("symbol", _safe_symbol(symbol)),
                   ("http", _http_code(error))), path=path)


def log_contract(contract, item, *, path=None):
    """Record a future's verified decision without copying Item.source.

    Source text is scanned for known markers only. Never interpolate it into
    log lines: upstream exceptions may contain arbitrary private data.
    """
    label = str(getattr(contract, "display", "") or "")
    label = label if _LABEL.fullmatch(label) else "-"
    symbol = _safe_symbol(getattr(item, "symbol", ""))
    description = str(getattr(item, "source", "") or "")
    if "вечный контракт" in description:
        status = "perpetual"
    elif not getattr(item, "verified", False):
        status = "unavailable"
    elif "экспирация не подтверждена" in description:
        status = "unknown_expiry"
    elif "переход за день" in description:
        status = "rolled"
    else:
        status = "verified"
    match = _DAY.search(description)
    expiry = "-"
    if match:
        try:
            expiry = date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            pass
    if "ICE Brent" in description:
        origin = "ice"
    elif "CME " in description:
        origin = "cme"
    elif "MOEX LASTTRADEDATE" in description:
        origin = "moex"
    elif "Finam GetAsset" in description:
        origin = "finam_asset"
    elif "Finam каталог" in description:
        origin = "finam_catalog"
    else:
        origin = "unconfirmed"
    if "поле даты отсутствует" in description:
        finam = "missing_field"
    elif "поле даты пустое" in description:
        finam = "empty_field"
    elif "недопустимом формате" in description:
        finam = "invalid_format"
    elif "Finam GetAsset" in description:
        finam = _http_code(description)
    else:
        finam = "none"
    calendar = ("unavailable" if "недоступен" in description
                else "unconfigured" if "не настроен" in description
                else "missing_series" if "нет точной даты" in description
                else "none")
    return _write((("event", "expiry_check"),
                   ("contract", label),
                   ("symbol", symbol),
                   ("status", status),
                   ("expiry", expiry),
                   ("source", origin),
                   ("finam", finam),
                   ("calendar", calendar),
                   ("http", _http_code(description))), path=path)
