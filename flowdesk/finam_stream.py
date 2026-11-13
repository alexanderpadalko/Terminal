"""Read-only Finam Trade API WebSocket transport (no trade subscriptions)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import threading

from PySide6.QtCore import QThread, Signal

from .asyncio_runtime import prepare_asyncio_runtime
from .finam_api import FinamAPI, FRAMES, decimal_number, finam_bar, symbol_name

WS_URL = "wss://api.finam.ru/ws"


@dataclass(frozen=True)
class FinamEvent:
    kind: str
    symbol: str
    board: str
    epoch: int
    received_at: datetime
    timeframe: str = ""
    candles: tuple = ()
    quote: dict = field(default_factory=dict)
    state: str = ""
    detail: str = ""


def interpret_packet(packet, ticker, mic, epoch, now):
    """Turn documented DATA envelopes into typed events; never confuse quotes with bars."""
    symbol = symbol_name(ticker,mic)
    def belongs(value):
        return isinstance(value,str) and value.upper()==symbol
    ptype = packet.get("type")
    if ptype == "ERROR":
        info = packet.get("error_info") or {}
        return FinamEvent("status",ticker,mic,epoch,now,state="unavailable",
                          detail=str(info.get("message") or info.get("type") or "Ошибка подписки"))
    if ptype != "DATA":
        return None
    subscription = packet.get("subscription_type")
    payload = packet.get("payload") or {}
    # The live gateway also sends payload as JSON text inside the JSON
    # envelope, despite the object representation in the AsyncAPI schema.
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    if subscription == "QUOTES":
        quotes = payload.get("quote") or []
        q = next((q for q in quotes if belongs(q.get("symbol"))), None)
        if q is not None:
            q = dict(q)
            if q.get("last") is not None:
                q["last"] = decimal_number(q["last"])
            return FinamEvent("quote",ticker,mic,epoch,now,quote=q)
        return None
    if subscription != "BARS":
        return None
    if not belongs(payload.get("symbol", symbol)):
        return None
    items = payload.get("bars") or []
    if isinstance(items, dict):
        items = [items]
    tf_codes = {v:k for k,v in FRAMES.items()}
    grouped = {}
    for row in items:
        if not isinstance(row, dict) or not belongs(row.get("symbol",symbol)):
            continue
        tf = tf_codes.get(row.get("timeframe") or packet.get("timeframe"))
        if tf is None:
            # The server often encodes the timeframe in subscription_key.
            key = str(packet.get("subscription_key") or "")
            tf = next((name for name, code in FRAMES.items()
                       if code in key), None)
        if tf is None:
            # Prefer explicit transport knowledge over guessed bar intervals.
            continue
        grouped.setdefault(tf, []).append(finam_bar(row, tf))
    return [FinamEvent("candles",ticker,mic,epoch,now,timeframe=tf,candles=tuple(bars))
            for tf,bars in grouped.items()] if grouped else None


class FinamStreamWorker(QThread):
    event = Signal(object)

    def __init__(self, ticker, mic, *, api=None, parent=None):
        super().__init__(parent)
        self.ticker, self.mic = ticker.upper(), mic.upper()
        self.api = api or FinamAPI()
        self._loop = self._task = None
        self._stopped = threading.Event()

    def stop(self):
        self._stopped.set()
        if self._loop is not None and self._task is not None:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass

    async def _connect_loop(self):
        from websockets.asyncio.client import connect
        epoch, failures = 0, 0
        while not self._stopped.is_set():
            epoch += 1
            now = lambda: datetime.now(timezone.utc)
            def status(state, detail=""):
                self.event.emit(FinamEvent("status",self.ticker,self.mic,epoch,now(),
                                           state=state,detail=detail))
            status("connecting")
            try:
                token = await asyncio.to_thread(self.api.access_key, force=epoch>1)
                async with connect(WS_URL,open_timeout=15,close_timeout=3,
                                   additional_headers={"Authorization": token},
                                   ping_interval=20,ping_timeout=15,max_size=4*1024*1024) as ws:
                    handshake = False
                    subscribed = False
                    ready = False
                    while not self._stopped.is_set():
                        raw = await asyncio.wait_for(ws.recv(),timeout=50)
                        data = json.loads(raw)
                        if data.get("type") == "EVENT":
                            event_name = (data.get("event_info") or {}).get("event")
                            if event_name == "HANDSHAKE_SUCCESS" and not subscribed:
                                handshake = subscribed = True
                                status("resync")
                                symbol = self.api.exchange_symbol(symbol_name(self.ticker,self.mic))
                                # Quotes and OHLC only; ORDERS/TRADES/ACCOUNT are never requested.
                                requests = [{"action":"SUBSCRIBE","type":"QUOTES",
                                             "data":{"symbols":[symbol]},"token":token}]
                                for tf in FRAMES.values():
                                    requests.append({"action":"SUBSCRIBE","type":"BARS",
                                                     "data":{"symbol":symbol,"timeframe":tf},
                                                     "token":token})
                                for req in requests:
                                    await ws.send(json.dumps(req,separators=(",",":")))
                            if event_name == "CONNECTION_CLOSED":
                                raise ConnectionError("Finam закрыл WebSocket")
                            continue
                        result = interpret_packet(data,self.ticker,self.mic,epoch,now())
                        if result is None:
                            continue
                        events = result if isinstance(result,list) else [result]
                        for event in events:
                            self.event.emit(event)
                            if event.kind == "status" and event.state == "unavailable":
                                raise PermissionError(event.detail)
                            if event.kind in ("quote","candles") and not ready:
                                ready=True
                                failures=0
                                status("ready")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log token, Authorization headers or raw server payload.
                msg = f"{type(exc).__name__}: {str(exc)[:180]}"
                if isinstance(exc,PermissionError):
                    status("unavailable",msg)
                    return
                status("reconnecting",msg)
                failures += 1
                await asyncio.sleep(min(30,2**min(failures,5)))

    def run(self):
        # Both JWT retrieval via to_thread and WebSocket DNS need the concrete
        # executor export in frozen Python 3.15, before an event loop starts.
        prepare_asyncio_runtime()
        async def runner():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            try:
                if not self._stopped.is_set():
                    await self._connect_loop()
            except asyncio.CancelledError:
                pass
            finally:
                self._loop = self._task = None
        asyncio.run(runner())
