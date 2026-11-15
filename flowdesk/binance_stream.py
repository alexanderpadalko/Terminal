"""Public USD-M kline/trade streams; no credentials or user subscriptions."""
import asyncio
from datetime import datetime,timezone
import json
from math import isfinite

from .binance_api import BinanceAPI,INTERVALS,instant,kline
from .finam_stream import FinamStreamWorker,FinamEvent
from .market_symbols import BINANCE,symbol_name

WS_URL="wss://fstream.binance.com/market/stream"


def interpret_packet(packet,ticker,epoch,now):
    data=packet.get("data",packet)
    if not isinstance(data,dict) or data.get("s")!=ticker:return None
    if data.get("e")=="aggTrade":
        price=float(data["p"])
        if not isfinite(price) or price<=0:raise ValueError("Invalid Binance LAST")
        return FinamEvent("quote",ticker,BINANCE,epoch,now,quote={
            "symbol":symbol_name(ticker,BINANCE),"last":price,
            "timestamp":instant(data["T"]).isoformat()})
    if data.get("e")!="kline":return None
    row=data.get("k",{})
    if row.get("s")!=ticker:return None
    tf=next((tf for tf,code in INTERVALS.items() if code==row.get("i")),None)
    if tf is None:return None
    bar=kline([row["t"],row["o"],row["h"],row["l"],row["c"],row["v"],row["T"]])
    return FinamEvent("candles",ticker,BINANCE,epoch,now,timeframe=tf,candles=(bar,))


class BinanceStreamWorker(FinamStreamWorker):
    def __init__(self,ticker,*,api=None,parent=None):
        symbol_name(ticker,BINANCE)
        super().__init__(ticker,BINANCE,api=api or BinanceAPI(),parent=parent)

    async def _connect_loop(self):
        from websockets.asyncio.client import connect
        epoch=failures=0
        ticker=self.ticker.lower()
        streams=[ticker+"@aggTrade",*(ticker+"@kline_"+code for code in INTERVALS.values())]
        url=WS_URL+"?streams="+"/".join(streams)
        while not self._stopped.is_set():
            epoch+=1;ready=False
            def status(state):
                self.event.emit(FinamEvent("status",self.ticker,BINANCE,epoch,
                    datetime.now(timezone.utc),state=state,
                    detail="Binance: соединение недоступно" if state=="reconnecting" else ""))
            status("connecting")
            try:
                await asyncio.to_thread(self.api._contract,symbol_name(self.ticker,BINANCE))
                async with connect(url,open_timeout=15,close_timeout=3,
                        ping_interval=20,ping_timeout=15,max_size=1024*1024) as ws:
                    status("resync")
                    while not self._stopped.is_set():
                        raw=await asyncio.wait_for(ws.recv(),timeout=50)
                        event=interpret_packet(json.loads(raw),self.ticker,epoch,datetime.now(timezone.utc))
                        if event is not None:
                            self.event.emit(event)
                            if not ready:
                                ready=True;failures=0;status("ready")
            except asyncio.CancelledError:raise
            except Exception:
                if self._stopped.is_set():return
                failures+=1;status("reconnecting")
                await asyncio.sleep(min(30,2**min(failures,5)))
