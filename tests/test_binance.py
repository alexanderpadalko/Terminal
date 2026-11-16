"""Public Binance USD-M isolation, paging, native candles and stream identity."""
from datetime import datetime,timedelta,timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit,parse_qs

from flowdesk.binance_api import BinanceAPI,BinanceAPIError,kline,INTERVALS
from flowdesk.binance_stream import interpret_packet
from flowdesk.market_services import MarketServices
from flowdesk.market_symbols import symbol_name
from flowdesk.finam_api import symbol_name as finam_symbol_name
from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_requests import RequestCoordinator
from flowdesk.finam_screener import FinamScreenerWorker,ScanResult
from flowdesk.finam_universe import Item
from flowdesk.market_data import Candle
from flowdesk.exchange_time import exchange_clock

NOW=datetime(2026,11,15,10,tzinfo=timezone.utc)
START=int(NOW.timestamp()*1000)


def contract(ticker="BTCUSDT",**extra):
    return {"symbol":ticker,"contractType":"PERPETUAL","status":"TRADING",
            "quoteAsset":"USDT","marginAsset":"USDT",
            "filters":[{"filterType":"PRICE_FILTER","tickSize":"0.10"}],**extra}


def row(start=START,close=None):
    return [start,"100","110","90","105","25",close or start+59999]


class Response:
    def __init__(self,value):self.value=value
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return json.dumps(self.value).encode()


class TestBinance(unittest.TestCase):
    def api(self,**kwargs):
        return BinanceAPI(clock=lambda:NOW,coordinator=RequestCoordinator(per_minute=60000),**kwargs)

    def test_archive_boundary_is_first_real_kline_and_is_cached_per_timeframe(self):
        requests=[]
        first=datetime(2019,9,8,tzinfo=timezone.utc)
        def opened(req,timeout):
            requests.append(req)
            if 'exchangeInfo' in req.full_url:
                return Response({'symbols':[contract(onboardDate=START)]})
            query=parse_qs(urlsplit(req.full_url).query)
            self.assertEqual(query['startTime'],['0'])
            self.assertEqual(query['limit'],['1'])
            return Response([row(int(first.timestamp()*1000))])
        api=self.api(opener=opened)
        self.assertEqual(api.archive_start('BTCUSDT@BINANCE','M1'),first)
        self.assertEqual(api.archive_start('BTCUSDT@BINANCE','M1'),first)
        self.assertEqual(len(requests),2)
        api.archive_start('BTCUSDT@BINANCE','H1')
        self.assertEqual(len(requests),3)

    def test_provider_identity_cannot_escape_two_perpetuals_or_finam_validation(self):
        self.assertEqual(symbol_name("btcusdt.p","BINANCE"),"BTCUSDT@BINANCE")
        for ticker in ("BTCUSD","BTCUSDT_261225","SOLUSDT","BTCUSDT/../../order"):
            with self.assertRaises(ValueError):symbol_name(ticker,"BINANCE")
        with self.assertRaises(ValueError):finam_symbol_name("BTCUSDT","BINANCE")

    def test_only_public_gets_without_credentials_and_exact_native_h4(self):
        requests=[]
        def opened(req,timeout):
            requests.append(req)
            path=urlsplit(req.full_url).path
            if path.endswith("exchangeInfo"):return Response({"symbols":[contract()]})
            if path.endswith("price"):return Response({"symbol":"BTCUSDT","price":"105","time":START})
            return Response([row(START,START+4*3600000-1)])
        api=self.api(opener=opened)
        quotes=api.latest_quote("BTCUSDT@BINANCE")
        bars=api.bars("BTCUSDT@BINANCE","H4",start=NOW,end=NOW+timedelta(hours=4))
        self.assertEqual(quotes["symbol"],"BTCUSDT@BINANCE")
        self.assertEqual(bars[0].scheduled_close,NOW+timedelta(hours=4))
        self.assertEqual(api.asset_tick_size("BTCUSDT@BINANCE"),.1)
        self.assertEqual(parse_qs(urlsplit(requests[-1].full_url).query)["interval"],["4h"])
        self.assertTrue(all(req.get_method()=="GET" and not req.get_header("Authorization")
                            and not req.get_header("X-mbx-apikey") for req in requests))
        before=len(requests)
        for path in ("/fapi/v1/order","/fapi/v2/account","/api/v3/klines"):
            with self.assertRaises(ValueError):api._json(path)
        self.assertEqual(len(requests),before)

    def test_wrong_contract_type_or_inactive_contract_never_fetches_candles(self):
        for values in ({"contractType":"CURRENT_QUARTER"},{"status":"SETTLING"},
                       {"marginAsset":"BTC"}):
            opened=Mock(return_value=Response({"symbols":[contract(**values)]}))
            with self.assertRaises(BinanceAPIError):self.api(opener=opened).bars("BTCUSDT@BINANCE","H1")
            self.assertEqual(opened.call_count,1)

    def test_pagination_is_complete_half_open_and_advances(self):
        calls=[]
        def opened(req,timeout):
            path=urlsplit(req.full_url).path
            if path.endswith("exchangeInfo"):return Response({"symbols":[contract()]})
            params=parse_qs(urlsplit(req.full_url).query);calls.append(params)
            first=int(params["startTime"][0])
            return Response([row(START+i*60000) for i in range(1000)] if first==START else
                            [row(START+1000*60000)])
        bars=self.api(opener=opened).bars("BTCUSDT@BINANCE","M1",start=NOW,
                                        end=NOW+timedelta(minutes=1001))
        self.assertEqual(len(bars),1001)
        self.assertEqual(calls[1]["startTime"],[str(START+999*60000+1)])
        self.assertEqual(calls[0]["endTime"],[str(START+1001*60000-1)])

    def test_monthly_close_comes_from_exchange_and_invalid_ohlc_is_rejected(self):
        closed=datetime(2026,12,1,tzinfo=timezone.utc)
        self.assertEqual(kline(row(START,int(closed.timestamp()*1000)-1)).scheduled_close,closed)
        bad=row();bad[2]="80"
        with self.assertRaises(ValueError):kline(bad)

    def test_wrong_symbol_quote_is_rejected(self):
        def opened(req,timeout):
            return Response({"symbols":[contract()]} if "exchangeInfo" in req.full_url else
                            {"symbol":"ETHUSDT","price":"105","time":START})
        with self.assertRaises(BinanceAPIError):self.api(opener=opened).latest_quote("BTCUSDT@BINANCE")

    def test_http_errors_are_redacted_and_preserve_status(self):
        def opened(req,timeout):
            raise HTTPError(req.full_url,451,"private detail",{},BytesIO(b"private response"))
        with self.assertRaises(BinanceAPIError) as caught:self.api(opener=opened)._contract("BTCUSDT@BINANCE")
        self.assertEqual(caught.exception.http_status,451)
        self.assertNotIn("private",str(caught.exception))

    def test_combined_streams_preserve_symbol_time_and_native_frame(self):
        for tf,code in INTERVALS.items():
            data={"e":"kline","s":"BTCUSDT","k":{"s":"BTCUSDT","i":code,
                  "t":START,"T":START+59999,"o":"100","h":"110","l":"90","c":"105","v":"25"}}
            event=interpret_packet({"stream":"btcusdt@kline_"+code,"data":data},"BTCUSDT",2,NOW)
            self.assertEqual((event.board,event.timeframe,event.epoch),("BINANCE",tf,2))
            self.assertEqual(event.candles[0].time,NOW)
            self.assertIsNone(interpret_packet(data,"ETHUSDT",2,NOW))
        quote=interpret_packet({"e":"aggTrade","s":"BTCUSDT","p":"105","T":START},"BTCUSDT",1,NOW)
        self.assertEqual(quote.quote["last"],105)
        self.assertEqual(quote.quote["timestamp"],NOW.isoformat())

    def test_crypto_utc_session_includes_weekends(self):
        clock=exchange_clock("BINANCE","BTCUSDT")
        self.assertTrue(clock.known)
        self.assertEqual(clock.zone.key,"UTC")
        self.assertEqual(clock.vwap_session_key(NOW),NOW.date())

    def test_crypto_and_finam_are_stored_and_scanned_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            finam=FinamMarketService(Path(temp)/"finam.sqlite3",api=SimpleNamespace())
            crypto=FinamMarketService(Path(temp)/"binance.sqlite3",api=self.api(opener=Mock()))
            services=MarketServices(finam=finam,crypto=crypto,directory=temp)
            bar=Candle(NOW,100,110,90,105,25)
            crypto._upsert("BTCUSDT@BINANCE","H1",[bar])
            self.assertFalse(finam._series("BTCUSDT@BINANCE"))
            seen=[]
            def evaluate(service,ticker,venue,settings,**kwargs):
                seen.append((service,ticker,venue));return ScanResult(ticker+"@"+venue,(0,)*5)
            entries=[Item("BTCUSDT.P","Bitcoin","crypto","BTCUSDT@BINANCE",True),
                     Item("SBER","Sber","stocks","SBER@MISX",True)]
            worker=FinamScreenerWorker(services,entries,object(),evaluate=evaluate,backfill=False)
            worker.run()
            self.assertIn((crypto,"BTCUSDT","BINANCE"),seen)
            self.assertIn((finam,"SBER","MISX"),seen)
