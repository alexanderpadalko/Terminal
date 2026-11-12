"""Finam-only adapters: network permission, exact request wire format, and GUI."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from flowdesk.finam_api import (FinamAPI, FinamAPIError, finam_bar,
                                symbol_name, FRAMES)
from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_stream import interpret_packet, FinamEvent
from flowdesk.market_data import Candle
from flowdesk.settings import IndicatorSettings

NOW = datetime(2026,11,13,12,tzinfo=timezone.utc)


def bar(time, n=100):
    return Candle(time,n,n+2,n-2,n+1,100,scheduled_close=time+timedelta(hours=1))


class FakeAPI:
    def __init__(self):
        self.calls=[]
    def market_delay_minutes(self,mic):
        return 0
    def bars(self,symbol,tf,**kwargs):
        self.calls.append((symbol,tf))
        step={"M1":timedelta(minutes=1),"M5":timedelta(minutes=5),
              "H1":timedelta(hours=1),"H4":timedelta(hours=4),
              "D1":timedelta(days=1),"W1":timedelta(days=7),
              "MN1":timedelta(days=28)}[tf]
        rows=[]
        for i in range(40):
            t=NOW - 40*step + i*step
            rows.append(Candle(t,99,110,95,101,200,
                               scheduled_close=t+step))
        return rows


class TestFinamAPI(unittest.TestCase):
    def test_ticker_mic_required(self):
        self.assertEqual(symbol_name("aapl","xnas"),"AAPL@XNAS")
        for pair in (("AAPL",""),("a/b","xnas"),("AAPL","TQBR;;;")):
            with self.assertRaises(ValueError): symbol_name(*pair)

    def test_bar_parses_both_string_and_protobuf_timestamp(self):
        payload={"timestamp":"2026-11-13T10:00:00Z",
                 "open":{"value":"10"},"high":{"value":"12"},
                 "low":{"value":"9"},"close":{"value":"11"},
                 "volume":{"value":"400"}}
        first=finam_bar(payload,"H1")
        second=finam_bar({**payload,"timestamp":{"seconds":1794564000,"nanos":0}},"H1")
        self.assertEqual((first.open,first.high,first.close,first.volume),(10,12,11,400))
        self.assertEqual((first.scheduled_close-first.time),timedelta(hours=1))
        self.assertEqual(second.time.tzinfo,timezone.utc)

    def test_auth_creates_bearer_and_only_gets_bars(self):
        paths=[]
        def fake_open(request,timeout):
            paths.append((request.get_method(),request.full_url,
                          request.get_header("Authorization")))
            if request.full_url.endswith("/sessions"):
                value={"token":"access_dummy"}
            elif request.full_url.endswith("/sessions/details"):
                value={"md_permissions":[{"mic":"XNAS","delay_minutes":0}]}
            elif "/bars?" in request.full_url:
                value={"bars":[]}
            else:raise AssertionError("unexpected endpoint")
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):return None
                def read(self):return json.dumps(value).encode()
            return Response()
        api=FinamAPI(opener=fake_open,secret_reader=lambda:"secret_mock",clock=lambda:NOW)
        self.assertEqual(api.bars("AAPL@XNAS","H1"),[])
        self.assertEqual(api.market_delay_minutes("XNAS"),0)
        self.assertEqual(paths[0][0],"POST")
        self.assertIn("/v1/sessions",paths[0][1])
        self.assertIn("timeframe=TIME_FRAME_H1",paths[-1][1])
        self.assertEqual(paths[-1][2],"Bearer access_dummy")

    def test_denies_post_orders_even_with_token(self):
        api=FinamAPI(secret_reader=lambda:"test")
        with self.assertRaisesRegex(ValueError,"writes are disabled"):
            api._json("POST","/v1/accounts/any/orders",{"side":"SIDE_BUY"})


class TestFinamMarket(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.api=FakeAPI()
        self.service=FinamMarketService(Path(self.dir.name)/"finam.sqlite3",
                                        api=self.api,clock=lambda:NOW)
    def test_http_off_does_not_call_history(self):
        with self.assertRaisesRegex(RuntimeError,"Разрешите HTTP"):
            self.service.view("AAPL","XNAS","H1",allow_http=False)
        self.assertEqual(self.api.calls,[])

    def test_allow_http_seed_then_cache_without_network(self):
        first,_,_=self.service.view("AAPL","XNAS","H1",allow_http=True)
        self.assertGreaterEqual(len(self.api.calls),7)
        self.api.calls.clear()
        again,_,_=self.service.view("AAPL","XNAS","H1",allow_http=False)
        self.assertEqual(first.candles,again.candles)
        self.assertEqual(self.api.calls,[])

    def test_stream_upserts_without_http(self):
        self.service.view("AAPL","XNAS","H1",allow_http=True)
        self.api.calls.clear()
        t=NOW-timedelta(hours=1)
        event=FinamEvent("candles","AAPL","XNAS",1,NOW,"H1",(bar(t,100),))
        self.service.ingest_stream("AAPL","XNAS",(event,))
        result,_,_=self.service.view("AAPL","XNAS","H1",allow_http=False)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(result.candles[-1].time,t)
        self.assertEqual(result.candles[-1].close,101)


class TestFinamWebSocket(unittest.TestCase):
    def test_quote_and_candles_are_distinct(self):
        quote={"type":"DATA","subscription_type":"QUOTES",
               "payload":{"quote":[{"symbol":"AAPL@XNAS","last":"250.2"}]}}
        event=interpret_packet(quote,"AAPL","XNAS",1,NOW)
        self.assertEqual(event.kind,"quote")
        candle={"timestamp":"2026-11-13T10:00:00Z",
                "open":"250","high":"252","low":"249","close":"251","volume":"23"}
        payload={"type":"DATA","subscription_type":"BARS",
                 "subscription_key":"AAPL@XNAS:TIME_FRAME_H1",
                 "payload":{"bars":[candle]}}
        batch=interpret_packet(payload,"AAPL","XNAS",1,NOW)
        self.assertEqual(batch[0].kind,"candles")
        self.assertEqual(batch[0].timeframe,"H1")
        self.assertEqual(batch[0].candles[0].high,252)
    def test_order_envelopes_ignored(self):
        self.assertIsNone(interpret_packet({"type":"DATA","subscription_type":"ORDERS",
                            "payload":{"orders":[{"id":"x"}]}}, "AAPL","XNAS",1,NOW))


if __name__=="__main__":
    unittest.main()
