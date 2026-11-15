"""Private, rotating, allow-listed diagnostics for the read-only FlowDesk terminal.

No API response, exception message, URL, token, account number or candle payload
is ever written. Only known event codes and parsed non-secret metadata survive.
The file lives in the user's local profile and is never sent to GitHub.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from logging import Formatter, INFO, LogRecord
from logging.handlers import RotatingFileHandler
from pathlib import Path
import os
import re
import threading
import time
import json
import math
import socket
import ssl
from urllib.error import HTTPError, URLError

LOG_PATH = Path.home() / ".flowdesk" / "logs" / "diagnostics.log"
_MAX_BYTES = 1_000_000
_BACKUPS = 3
_LOCK = threading.RLock()
_EVENTS = frozenset({
    "catalog_start", "catalog_complete", "catalog_unavailable",
    "stock_ranking_unavailable", "moex_expiry_unavailable",
    "catalog_worker_failed", "chart_load_failed",
    "stream_unavailable", "stream_reconnecting",
    "storage_snapshot_unavailable",
    "futures_snapshot_unavailable", "asset_details_unavailable",
})
_STAGES=frozenset(("catalog_refresh","catalog","auth","token_details","asset_details",
    "futures_resolution","roll_state_load","roll_state_save","futures_snapshot",
    "stock_ranking","catalog_publish","chart_load","websocket","backup","candles","quotes"))
_KINDS=frozenset(("http","timeout","dns","tls","connection","network",
                  "invalid_response","cancelled","storage","internal","unknown"))
_TYPES=frozenset(("FinamAPIError","BinanceAPIError","HTTPError","URLError","TimeoutError",
    "gaierror","SSLError","SSLCertVerificationError","ConnectionError","ConnectionResetError",
    "ConnectionRefusedError","ConnectionAbortedError","BrokenPipeError","JSONDecodeError",
    "UnicodeDecodeError","ValueError","TypeError","AttributeError","KeyError","RuntimeError",
    "OSError","PermissionError","FileNotFoundError","NotADirectoryError","IsADirectoryError",
    "DatabaseError","OperationalError","IntegrityError","InterruptedError"))
_OUTCOMES=frozenset(("ok","partial","fallback","failed","cancelled"))
_SYMBOL = re.compile(r"^[A-Za-z0-9_.-]{1,24}@(?:[A-Z0-9]{4}|BINANCE)$")
_LABEL = re.compile(r"^[A-Z0-9.]{2,24}$")
_DAY = re.compile(r"(?:действует до |новая дата |экспирация )(\d{4}-\d{2}-\d{2})")
_HTTP = re.compile(r"\bHTTP\s+([1-5]\d{2})\b", re.IGNORECASE)


def _safe_symbol(value):
    text = str(value or "")
    return text if _SYMBOL.fullmatch(text) else "-"


def _http_code(value):
    code=getattr(value,"http_status",None)
    if code is None and isinstance(value,HTTPError):code=value.code
    if type(code) is int and 100<=code<=599:return "http_"+str(code)
    # Legacy callers may supply text, but exception objects are NEVER
    # stringified: even __str__ can contain credentials or fail itself.
    match = _HTTP.search(value) if isinstance(value,str) else None
    return "http_" + match.group(1) if match else "none"


def _error_details(error):
    """Classify only known types/attributes and a bounded causal chain."""
    if error is None:return "none","none","none"
    node=error;seen=set();fallback=("unknown","other","none")
    for _ in range(8):
        if id(node) in seen:break
        seen.add(id(node))
        name=type(node).__name__
        name=name if name in _TYPES else "other"
        if isinstance(node,str):name=node if node in _TYPES else "other"
        http=_http_code(node)
        if http!="none":return "http",name,http
        explicit=getattr(node,"diagnostic_kind",None)
        if isinstance(explicit,str) and explicit in _KINDS:return explicit,name,http
        if isinstance(node,TimeoutError):return "timeout",name,http
        if isinstance(node,socket.gaierror):return "dns",name,http
        if isinstance(node,ssl.SSLError):return "tls",name,http
        if isinstance(node,ConnectionError):return "connection",name,http
        if isinstance(node,(json.JSONDecodeError,UnicodeDecodeError)):
            return "invalid_response",name,http
        if isinstance(node,InterruptedError):return "cancelled",name,http
        if isinstance(node,(PermissionError,FileNotFoundError,NotADirectoryError,IsADirectoryError)):
            return "storage",name,http
        if name in ("DatabaseError","OperationalError","IntegrityError"):return "storage",name,http
        if isinstance(node,URLError):
            fallback=("network",name,http)
            reason=node.reason
            if isinstance(reason,BaseException):
                node=reason;continue
            return fallback
        if isinstance(node,OSError):return "network",name,http
        fallback=(fallback[0],name if name!="other" else fallback[1],http)
        cause=getattr(node,"__cause__",None) or getattr(node,"__context__",None)
        if not isinstance(cause,BaseException):break
        node=cause
    return fallback


def _bounded_number(value,maximum):
    if type(value) in (int,float) and 0<=value<=maximum and math.isfinite(value):
        return str(round(value))
    return None


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


def log_event(event, *, symbol="", error=None, stage="", outcome="",elapsed_ms=None,path=None):
    """Record allowlisted failure context; never serialize a raw exception."""
    if event not in _EVENTS:
        return False
    kind,error_type,http=_error_details(error)
    if kind=="network" and stage in ("backup","futures_snapshot","roll_state_load","roll_state_save"):
        kind="storage"
    fields=[("event",event),("symbol",_safe_symbol(symbol)),("http",http)]
    if error is not None:fields.extend((("kind",kind),("error_type",error_type)))
    if isinstance(stage,str) and stage in _STAGES:fields.append(("stage",stage))
    operation=getattr(error,"diagnostic_operation",stage)
    if isinstance(operation,str) and operation in _STAGES:fields.append(("operation",operation))
    page=getattr(error,"diagnostic_page",None)
    if type(page) is int and 1<=page<=150:fields.append(("page",str(page)))
    retry=_bounded_number(getattr(error,"retry_after",None),86400)
    if retry is not None:fields.append(("retry_after_s",retry))
    elapsed=_bounded_number(elapsed_ms,3_600_000)
    if elapsed is not None:fields.append(("elapsed_ms",elapsed))
    if isinstance(outcome,str) and outcome in _OUTCOMES:fields.append(("outcome",outcome))
    return _write(fields,path=path)


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


def log_snapshot(*, symbol="", timeframe="", asof="", delay_seconds=None, path=None):
    """Record a safe chart observation, not a claim about actual feed latency."""
    period = timeframe if timeframe in ("M1","M5","H1","H4","D1","W1","MN1") else "-"
    stamp = "-"
    try:
        when = datetime.fromisoformat(str(asof).replace("Z","+00:00"))
        if when.tzinfo is not None and 2000 <= when.year <= 2100:
            stamp = when.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        pass
    delay = "-"
    if type(delay_seconds) is int and 0 <= delay_seconds <= 86400:
        delay = str(delay_seconds // 60)
    return _write((("event","chart_observation"),
                   ("symbol",_safe_symbol(symbol)),
                   ("timeframe",period),
                   ("asof_utc",stamp),
                   ("permission_delay_min",delay)),path=path)
