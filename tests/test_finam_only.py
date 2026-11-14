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
    def test_new_token_invalidates_prior_account_scoped_session_cache(self):
        api=FinamAPI(secret_reader=lambda:"local_secret")
        api._jwt="previous_access_key"
        api._expiry=float("inf")
        api._account_ids=["previous_account"]
        api.permissions=[{"mic":"XCME","delay_minutes":15}]
        api._asset_details={"NQZ26@XCME":{"expiration_date":"2026-12-18"}}
        api._asset_ticks={"NQZ26@XCME":0.25}
        api._exact_symbols={"NQZ26@XCME":"NQZ26@XCME"}
        api._asset_retry_after={"NQZ26@XCME":100000.0}
        api.invalidate_session()
        self.assertEqual(api._jwt,"")
        self.assertEqual(api._expiry,0.0)
        self.assertEqual(api._account_ids,[])
        self.assertEqual(api.permissions,[])
        self.assertEqual(api._asset_details,{})
        self.assertEqual(api._asset_ticks,{})
        self.assertEqual(api._asset_retry_after,{})
        self.assertEqual(api._exact_symbols,{})

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

    def test_bounded_history_request_uses_exact_inclusive_exclusive_dates(self):
        from urllib.parse import parse_qs, urlparse
        requests=[]
        def opener(request,timeout):
            requests.append(request.full_url)
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):return None
                def read(self):return b'{"bars":[]}'
            return Response()
        api=FinamAPI(opener=opener,secret_reader=lambda:"dummy")
        api._jwt,api._expiry="fake_session",float("inf")
        start=NOW-timedelta(days=120)
        self.assertEqual(api.bars("SIZ6@RTSX","W1",start=start,end=NOW),[])
        query=parse_qs(urlparse(requests[0]).query)
        self.assertEqual(query["timeframe"],["TIME_FRAME_W"])
        self.assertEqual(query["interval.start_time"],[start.isoformat().replace("+00:00","Z")])
        self.assertEqual(query["interval.end_time"],[NOW.isoformat().replace("+00:00","Z")])
        with self.assertRaises(ValueError):
            api.bars("SIZ6@RTSX","W1",start=NOW,end=start)
        self.assertEqual(len(requests),1)

    def test_finam_active_instruments_catalog_is_read_only_paginated(self):
        from urllib.parse import parse_qs, urlparse
        paths=[]
        def opener(req,timeout):
            paths.append((req.get_method(),req.full_url))
            if req.full_url.endswith("/v1/sessions"):
                body={"token":"fake_access"}
            elif req.full_url.endswith("/v1/sessions/details"):
                body={"md_permissions":[]}
            elif "/v1/assets/all?" in req.full_url:
                cursor=parse_qs(urlparse(req.full_url).query).get("cursor",[""])[0]
                if not cursor:
                    body={"assets":[{"symbol":"NQZ6@XCME","ticker":"NQZ6","mic":"XCME"}],
                          "next_cursor":"123"}
                else:
                    self.assertEqual(cursor,"123")
                    body={"assets":[{"symbol":"SiZ6@RTSX","ticker":"SiZ6","mic":"RTSX"}],
                          "next_cursor":""}
            else:
                self.fail("unexpected Finam API endpoint")
            class Resp:
                def __enter__(self):return self
                def __exit__(self,*unused):pass
                def read(self):return json.dumps(body).encode("utf-8")
            return Resp()
        api=FinamAPI(opener=opener,secret_reader=lambda:"fake_secret")
        result=api.active_assets()
        self.assertEqual(len(result),2)
        self.assertEqual(api.exchange_symbol("SIZ6@RTSX"),"SiZ6@RTSX")
        self.assertTrue(all(method in ("GET","POST") for method,_ in paths))
        self.assertTrue(all(method=="GET" for method,url in paths
                            if "/v1/assets/" in url))
        self.assertEqual(len([x for x in paths if "/v1/assets/all?" in x[1]]),2)

    def test_getasset_404_retries_once_with_only_authorized_account(self):
        from urllib.parse import parse_qs, urlparse
        requests=[]
        def opener(req,timeout):
            url=req.full_url
            requests.append((req.get_method(),url))
            if url.endswith("/v1/sessions"):
                body={"token":"private_session_token"}
            elif url.endswith("/v1/sessions/details"):
                body={"md_permissions":[],"account_ids":["private_account_1"]}
            elif "/v1/assets/NQZ26%40XCME" in url:
                query=parse_qs(urlparse(url).query)
                if not query:
                    raise HTTPError(url,404,"not found",{},
                                    BytesIO(b'{"secret":"never print"}'))
                self.assertEqual(query.get("account_id"),["private_account_1"])
                body={"future_details":{"expiration_date":"2026-12-18T12:00:00Z"}}
            else:
                self.fail("Unexpected Finam endpoint")
            class Response:
                def __enter__(self):return self
                def __exit__(self,*unused):return None
                def read(self):return json.dumps(body).encode()
            return Response()
        api=FinamAPI(opener=opener,secret_reader=lambda:"secret_only_local")
        result=api.asset_details("NQZ26@XCME")
        self.assertIn("future_details",result)
        asset_gets=[url for method,url in requests if method=="GET"]
        self.assertEqual(len(asset_gets),2)
        self.assertNotIn("secret_only_local",str(result))

    def test_asset_expiry_cache_is_cleared_for_recheck(self):
        state={"date":"2026-12-18","calls":0}
        def opener(req,timeout):
            state["calls"]+=1
            class Response:
                def __enter__(self):return self
                def __exit__(self,*unused):return None
                def read(self):
                    return json.dumps({"future_details":{
                        "expiration_date":state["date"]}}).encode()
            return Response()
        api=FinamAPI(opener=opener,secret_reader=lambda:"local")
        api._jwt,api._expiry="dummy",float("inf")
        self.assertEqual(api.asset_details("NQZ26@XCME")["future_details"]["expiration_date"],"2026-12-18")
        state["date"]="2026-12-19"
        self.assertEqual(api.asset_details("NQZ26@XCME")["future_details"]["expiration_date"],"2026-12-18")
        self.assertEqual(state["calls"],1)
        api.clear_asset_details_cache()
        self.assertEqual(api.asset_details("NQZ26@XCME")["future_details"]["expiration_date"],"2026-12-19")
        self.assertEqual(state["calls"],2)

    def test_getasset_never_guesses_account_or_retries_http_403(self):
        for ids,code in ((["a","b"],404),(["a"],403),([],404)):
            calls=[]
            def opener(req,timeout):
                calls.append(req.full_url)
                raise HTTPError(req.full_url,code,"permission error",{},BytesIO(b""))
            api=FinamAPI(opener=opener,secret_reader=lambda:"private")
            api._jwt,api._expiry="dummy",float("inf")
            api._account_ids=ids
            with self.assertRaises(FinamAPIError):
                api.asset_details("NQZ26@XCME")
            self.assertEqual(len(calls),1)
            self.assertNotIn("account_id=",calls[0])

    def test_cancelled_directory_lookup_does_not_contact_network(self):
        api=FinamAPI(opener=lambda *a,**k:self.fail("network must not be used"))
        with self.assertRaises(InterruptedError):
            api.active_assets(cancelled=lambda:True)

    def test_denies_post_orders_even_with_token(self):
        api=FinamAPI(secret_reader=lambda:"test")
        with self.assertRaisesRegex(ValueError,"writes are disabled"):
            api._json("POST","/v1/accounts/any/orders",{"side":"SIDE_BUY"})

    def test_history_404_is_actionable_without_echoing_response_or_key(self):
        def missing(request, timeout):
            raise HTTPError(request.full_url, 404, "missing", {},
                            BytesIO(b'{"message":"sensitive_secret"}'))
        api=FinamAPI(opener=missing, secret_reader=lambda:"sensitive_secret")
        api._jwt,api._expiry="access_dummy",float("inf")
        with self.assertRaises(FinamAPIError) as caught:
            api.bars("TSLA@XNAS","H1")
        self.assertEqual(caught.exception.http_status,404)
        self.assertIn("MIC",str(caught.exception))
        self.assertNotIn("sensitive_secret",str(caught.exception))
        self.assertNotIn("access_dummy",str(caught.exception))


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

    def test_unavailable_selected_instrument_stops_after_one_request(self):
        def missing(symbol,tf,**kwargs):
            self.api.calls.append((symbol,tf))
            raise FinamAPIError("Finam HTTP 404: проверьте тикер, MIC и доступ токена.",http_status=404)
        self.api.bars=missing
        with self.assertRaisesRegex(RuntimeError,"TSLA@XNAS · H1: Finam HTTP 404"):
            self.service.view("TSLA","XNAS","H1",allow_http=True)
        self.assertEqual(self.api.calls,[("TSLA@XNAS","H1")])

    def test_failed_refresh_reports_stale_history_and_preserves_cache(self):
        first,_,_=self.service.view("SBER","MISX","H1",allow_http=True)
        def offline(*args,**kwargs):
            raise FinamAPIError("Finam HTTP 503")
        self.api.bars=offline
        again,_,info=self.service.view("SBER","MISX","H1",allow_http=True,refresh=True)
        self.assertEqual(first.candles,again.candles)
        self.assertIn("Не обновлены",info["warning"])
        self.assertIn("H1",info["warning"])


class TestFinamWebSocket(unittest.TestCase):
    def test_live_gateway_json_string_payload_and_wrapped_decimal(self):
        packet={"type":"DATA","subscription_type":"QUOTES","subscription_key":"TSLA@XNGS",
            "payload":json.dumps({"quote":[{"symbol":"TSLA@XNGS","last":{"value":"250.2"}}]})}
        event=interpret_packet(packet,"TSLA","XNGS",1,NOW)
        self.assertEqual(event.quote["last"],250.2)
        candle={"timestamp":"2026-11-13T10:00:00Z","open":{"value":"250"},
                "high":{"value":"252"},"low":{"value":"249"},
                "close":{"value":"251"},"volume":{"value":"23"}}
        packet={"type":"DATA","subscription_type":"BARS",
            "subscription_key":"TSLA@XNGS:TIME_FRAME_H1",
            "payload":json.dumps({"symbol":"TSLA@XNGS","bars":[candle]})}
        result=interpret_packet(packet,"TSLA","XNGS",1,NOW)
        self.assertEqual(result[0].timeframe,"H1")
        self.assertEqual(result[0].candles[0].close,251)
        self.assertIsNone(interpret_packet(packet,"SBER","MISX",1,NOW))

    def test_mixed_case_catalog_ticker_is_accepted_in_live_packets(self):
        from flowdesk.finam_stream import interpret_packet
        instant="2026-11-13T10:00:00Z"
        quote={"type":"DATA","subscription_type":"QUOTES",
               "payload":{"quote":[{"symbol":"SiZ6@RTSX","last":{"value":"82990"}}]}}
        parsed=interpret_packet(quote,"SIZ6","RTSX",1,NOW)
        self.assertEqual(parsed.kind,"quote")
        self.assertEqual(parsed.quote["last"],82990.0)
        candle={"symbol":"SiZ6@RTSX","timeframe":"TIME_FRAME_H1",
                "timestamp":instant,"open":"1","high":"2",
                "low":"1","close":"2","volume":"1"}
        packet={"type":"DATA","subscription_type":"BARS",
                "payload":{"symbol":"SiZ6@RTSX","bars":[candle]}}
        batch=interpret_packet(packet,"SIZ6","RTSX",1,NOW)
        self.assertEqual(len(batch),1)
        self.assertEqual(batch[0].kind,"candles")

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
