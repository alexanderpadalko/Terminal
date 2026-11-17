"""Public USD-M perpetual market data only. No keys or trading endpoints."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
import math
import threading
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
from urllib.request import Request,urlopen

from .finam_api import FinamAPIError,DEPTH_DAYS
from .finam_requests import RequestCoordinator
from .market_data import Candle,normalize
from .market_symbols import BINANCE,symbol_name

BASE_URL="https://fapi.binance.com"
INTERVALS={"M1":"1m","M5":"5m","H1":"1h","H4":"4h",
           "D1":"1d","W1":"1w","MN1":"1M"}
ENDPOINTS=frozenset(("/fapi/v1/exchangeInfo","/fapi/v1/klines","/fapi/v2/ticker/price"))


class BinanceAPIError(FinamAPIError):
    pass


def instant(milliseconds):
    if isinstance(milliseconds,bool):raise ValueError("Invalid Binance timestamp")
    return datetime.fromtimestamp(int(milliseconds)/1000,tz=timezone.utc)


def kline(row):
    opened,closed=instant(row[0]),instant(row[6])+timedelta(milliseconds=1)
    values=tuple(float(value) for value in row[1:6])
    if (len(values)!=5 or not all(math.isfinite(v) for v in values) or
            min(values[:4])<=0 or values[4]<0 or
            values[1]<max(values[0],values[2],values[3]) or
            values[2]>min(values[0],values[1],values[3]) or closed<=opened):
        raise ValueError("Invalid Binance candle")
    return Candle(opened,*values,scheduled_close=closed)


class BinanceAPI:
    session_candles=True
    stream_frames=('M5','H1','H4','D1')
    candle_depth_days={**DEPTH_DAYS,"D1":5*365}
    provider_name="Binance"
    provenance_mode="BINANCE_USDM_NATIVE_CANDLES"
    permissions_status="public"

    def __init__(self,*,opener=None,clock=None,coordinator=None):
        self.opener=opener or urlopen
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        # Separate public-source budget. At limit=1000 the maximum is
        # 900 weight/min, below Binance's published 2400-weight budget.
        self.coordinator=coordinator or RequestCoordinator()
        self._metadata=None;self._metadata_at=0
        self._metadata_lock=threading.RLock()
        self._archive_starts={}

    def request_scope(self,**kwargs):return self.coordinator.scope(**kwargs)

    def _json(self,path,params=None):
        if path not in ENDPOINTS:
            raise ValueError("Only public Binance market-data GETs are allowed")
        query=urlencode(params or {})
        key=(path,query)
        def send():
            request=Request(BASE_URL+path+("?"+query if query else ""),
                headers={"Accept":"application/json","User-Agent":"FlowDesk/0.2.26"},method="GET")
            try:
                with self.opener(request,timeout=16) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                try:retry=float(exc.headers.get("Retry-After",60))
                except (ValueError,TypeError):retry=60
                raise BinanceAPIError("Binance HTTP "+str(exc.code),
                    http_status=exc.code,retry_after=retry if exc.code==429 else None) from None
            except URLError:
                raise BinanceAPIError("Binance: соединение недоступно") from None
        return self.coordinator.run(key,send)

    @staticmethod
    def _ticker(symbol):
        ticker,venue=symbol.rsplit("@",1)
        if venue!=BINANCE or symbol_name(ticker,venue)!=symbol:
            raise ValueError("Unknown Binance perpetual")
        return ticker

    def _contract(self,symbol):
        ticker=self._ticker(symbol)
        with self._metadata_lock:
            if self._metadata is None or time.monotonic()-self._metadata_at>=600:
                data=self._json("/fapi/v1/exchangeInfo")
                self._metadata={row["symbol"]:row for row in data.get("symbols",[])
                    if isinstance(row,dict) and row.get("symbol") in ("BTCUSDT","ETHUSDT")}
                self._metadata_at=time.monotonic()
            row=self._metadata.get(ticker,{})
            if (row.get("contractType")!="PERPETUAL" or row.get("status")!="TRADING"
                    or row.get("quoteAsset")!="USDT" or row.get("marginAsset")!="USDT"):
                raise BinanceAPIError("Binance: бессрочный USDT-контракт не подтверждён")
            return row

    def market_delay_minutes(self,venue):return 0

    def archive_start(self,symbol,timeframe):
        """First actual kline returned from the epoch, not a guessed listing date."""
        if timeframe not in INTERVALS:raise ValueError('Unknown Binance timeframe')
        self._contract(symbol)
        key=(symbol,timeframe)
        with self._metadata_lock:
            if key in self._archive_starts:return self._archive_starts[key]
        rows=self._json('/fapi/v1/klines',{'symbol':self._ticker(symbol),
            'interval':INTERVALS[timeframe],'startTime':0,'limit':1})
        if not isinstance(rows,list) or len(rows)>1:
            raise BinanceAPIError('Binance: неверная граница архива')
        first=kline(rows[0]).time if rows else None
        if first is not None:
            with self._metadata_lock:self._archive_starts[key]=first
        return first

    def asset_tick_size(self,symbol):
        row=self._contract(symbol)
        tick=next((r.get("tickSize") for r in row.get("filters",[])
                   if r.get("filterType")=="PRICE_FILTER"),None)
        value=Decimal(str(tick))
        return float(value) if value.is_finite() and value>0 else None

    def latest_quote(self,symbol):
        self._contract(symbol)
        row=self._json("/fapi/v2/ticker/price",{"symbol":self._ticker(symbol)})
        if row.get("symbol")!=self._ticker(symbol):
            raise BinanceAPIError("Binance: другая котировка")
        return {"symbol":symbol,"last":row["price"],"timestamp":instant(row["time"]).isoformat()}

    def bars(self,symbol,timeframe,*,now=None,start=None,end=None):
        if timeframe not in INTERVALS:raise ValueError("Unknown Binance timeframe")
        self._contract(symbol)
        upper=end or now or self.clock()
        lower=start or upper-timedelta(days=self.candle_depth_days[timeframe])
        if lower.tzinfo is None or upper.tzinfo is None or lower>=upper:
            raise ValueError("Invalid Binance interval")
        cursor=int(lower.timestamp()*1000);stop=int(upper.timestamp()*1000)
        result=[]
        for _ in range(100):
            if cursor>=stop:break
            rows=self._json("/fapi/v1/klines",{"symbol":self._ticker(symbol),
                "interval":INTERVALS[timeframe],"startTime":cursor,"endTime":stop-1,"limit":1000})
            if not isinstance(rows,list):raise BinanceAPIError("Binance: неверный ответ свечей")
            if not rows:break
            parsed=[kline(row) for row in rows]
            result.extend(bar for bar in parsed if lower<=bar.time<upper)
            following=max(int(row[0]) for row in rows)+1
            if following<=cursor:raise BinanceAPIError("Binance: повтор страницы свечей")
            cursor=following
            if len(rows)<1000:break
        else:
            raise BinanceAPIError("Binance: диапазон свечей не завершён")
        return normalize(result)
