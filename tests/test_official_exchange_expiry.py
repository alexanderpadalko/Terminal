"""Official exchange calendar fallback & sanitized Finam expiry diagnostics."""
import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from datetime import date, datetime, timezone
import json
import unittest

from flowdesk.exchange_expiry import (
    OfficialExpiry, cme_calendar_dates, ice_brent_dates, ICE_URL,
)
from flowdesk.finam_rotation import expiry_date, resolve_contract
from flowdesk.finam_universe import US_CONTRACTS


NOW=datetime(2026,11,13,17,0,tzinfo=timezone.utc)
US=US_CONTRACTS[0]


def asset(symbol,*,expires=None):
    ticker,mic=symbol.split("@")
    data={"symbol":symbol,"ticker":ticker,"mic":mic,"type":"TYPE_FUTURE"}
    if expires:
        data["expiration_date"]={"year":expires.year,"month":expires.month,"day":expires.day}
    return data


class _NoExpiryAPI:
    def __init__(self,*,error=None):
        self.error=error
        self.calls=[]

    def asset_details(self,symbol):
        self.calls.append(symbol)
        if self.error:
            raise self.error
        return {"ticker":symbol.split("@")[0],"expiration_date":None}


class _HTTP(Exception):
    def __init__(self,code,secret="secret-key-never-disclose"):
        self.http_status=code
        super().__init__()
        self.secret=secret


class TestOfficialExchangeCalendar(unittest.TestCase):
    def test_ice_brent_last_trade_date_not_first_notice_or_final_settlement(self):
        html="""<table><thead><tr>
          <th>Contract Symbol</th><th>FTD (First Trade Date)</th>
          <th>LTD (Last Trade Date)</th><th>FND (First Notice Date)</th>
          <th>FSD (Final Settlement Date)</th></tr></thead>
          <tbody><tr><td>Jan27</td><td>4/16/2019</td>
          <td>11/30/2026</td><td>11/29/2026</td>
          <td>12/1/2026</td></tr>
          <tr><td>Feb27</td><td>4/16/2019</td><td>12/30/2026</td>
          <td>12/29/2026</td><td>12/31/2026</td></tr></tbody></table>"""
        dates=ice_brent_dates(html)
        self.assertEqual(dates[(2027,1)],date(2026,11,30))
        self.assertEqual(dates[(2027,2)],date(2026,12,30))
        official=OfficialExpiry(fetch=lambda url:html,reference_year=2026)
        result,src=official.get("BRNF27@IFEU")
        self.assertEqual(result,date(2026,11,30))
        self.assertIn("ICE",src)
        self.assertEqual(official.get("BRNG27@IFEU")[0],date(2026,12,30))

    def test_ice_ltd_header_may_move_columns_but_settlement_is_not_expiry(self):
        html="""<table><tr><th>Contract Symbol</th>
        <th>FSD (Final Settlement Date)</th><th>LTD (Last Trade Date)</th></tr>
        <tr><td>Jan27</td><td>12/1/2026</td><td>11/30/2026</td></tr>
        </table>"""
        dates=ice_brent_dates(html)
        self.assertEqual(dates[(2027,1)],date(2026,11,30))

    def test_ice_rejects_conflicting_last_trading_days(self):
        html="""<table><tr><th>Contract Symbol</th><th>FTD</th>
        <th>LTD (Last Trade Date)</th></tr>
        <tr><td>Jan27</td><td>x</td><td>11/30/2026</td></tr>
        <tr><td>Jan27</td><td>x</td><td>12/1/2026</td></tr></table>"""
        self.assertNotIn((2027,1),ice_brent_dates(html))

    def test_ice_no_ltd_column_is_not_trusted(self):
        html="<table><tr><th>Contract Symbol</th><th>First Trade</th><th>Delivery Date</th></tr><tr><td>Jan27</td><td>x</td><td>11/30/2026</td></tr></table>"
        self.assertEqual(ice_brent_dates(html),{})

    def test_cme_future_calendar_uses_only_last_trade_and_exact_tenor(self):
        calendar={"contracts":[
            {"contractSymbol":"NQZ26","lastTradeDate":"2026-12-18",
             "firstNoticeDate":"2026-12-17"},
            {"contractSymbol":"NQH27","lastTradingDate":"2027-03-19"},
            {"contractSymbol":"MNQZ26","lastTradeDate":"2026-12-16"},
            {"contractSymbol":"GCZ26","lastTradeDate":"2026-12-29"},
            {"contractSymbol":"NQZ27","expirationDate":"2027-12-18"},
            {"contractSymbol":"NQM27","eventType":"Last Trade Date",
             "eventDate":"2027-06-17"},
            {"contractSymbol":"NQU27","eventType":"Final Settlement",
             "eventDate":"2027-09-17"},
        ]}
        dates=cme_calendar_dates(calendar,root="NQ",reference_year=2026)
        self.assertEqual(dates[(2026,12)],date(2026,12,18))
        self.assertEqual(dates[(2027,3)],date(2027,3,19))
        self.assertEqual(dates[(2027,6)],date(2027,6,17))
        self.assertNotIn((2027,9),dates)
        self.assertNotIn((2027,12),dates)

    def test_cme_rejects_micro_and_conflicting_contract_identity(self):
        calendar={"contracts":[
            {"contractSymbol":"MNQZ26","contractMonth":"202612",
             "lastTradeDate":"2026-12-17"},
            {"contractSymbol":"NQZ26","contractMonth":"202703",
             "lastTradeDate":"2026-12-18"},
            {"contractSymbol":"NQZ26","symbol":"ESZ26",
             "lastTradeDate":"2026-12-18"},
            {"contractSymbol":"NQH27","contractMonth":"202703",
             "lastTradeDate":"2027-03-19"}
        ]}
        dates=cme_calendar_dates(calendar,root="NQ",reference_year=2026)
        self.assertNotIn((2026,12),dates)
        self.assertEqual(dates[(2027,3)],date(2027,3,19))

    def test_cme_http_status_is_sanitized(self):
        from urllib.error import HTTPError
        def forbidden(_):
            raise HTTPError("https://secret.example/token=do-not-log",
                            403,"do-not-log",None,None)
        official=OfficialExpiry(fetch=forbidden,reference_year=2026)
        day,reason=official.get("NQZ26@XCME")
        self.assertIsNone(day)
        self.assertIn("HTTP 403",reason)
        self.assertNotIn("secret",reason)
        self.assertNotIn("do-not-log",reason)

    def test_finam_date_absence_is_specific_and_never_echoes_values(self):
        from flowdesk.finam_rotation import expiry_absence
        self.assertIn("отсутствует",expiry_absence({"ticker":"NQZ26"}))
        self.assertIn("пустое",expiry_absence({"future_details":{
            "expiration_date":None}}))
        self.assertIn("формате",expiry_absence({"future_details":{
            "expiration_date":"hidden-private-data"}}))
        self.assertNotIn("hidden-private-data",expiry_absence({
            "future_details":{"expiration_date":"hidden-private-data"}}))

    def test_empty_protobuf_timestamp_is_not_expiry(self):
        self.assertIsNone(expiry_date({"future_details":{
            "expiration_date":{"seconds":"0","nanos":0}
        }},mic="XCME"))
        self.assertIsNone(expiry_date({"future_details":{
            "expiration_date":{"seconds":"1","nanos":1000000000}
        }},mic="XCME"))

    def test_cme_official_get_uses_exchange_domain_and_fetches_once(self):
        calls=[]
        def fetch(url):
            calls.append(url)
            return json.dumps({"events":[
                {"symbol":"NQZ26","lastTradeDate":"2026-12-18"},
                {"symbol":"NQH27","lastTradeDate":"2027-03-19"}
            ]}).encode()
        official=OfficialExpiry(fetch=fetch,reference_year=2026)
        self.assertEqual(official.get("NQZ26@XCME")[0],date(2026,12,18))
        self.assertEqual(official.get("NQH27@XCME")[0],date(2027,3,19))
        self.assertEqual(len(calls),1)
        self.assertTrue(calls[0].startswith("https://www.cmegroup.com/"))
        self.assertIn("/ProductCalendar/Future/",calls[0])

    def test_cme_404_tries_published_non_json_route(self):
        class NotFound(Exception):
            code=404
        calls=[]
        def fetch(url):
            calls.append(url)
            if url.endswith(".json"):
                raise NotFound()
            return json.dumps({"contracts":[{
                "contractSymbol":"NQZ26","lastTradeDate":"2026-12-18"
            }]})
        official=OfficialExpiry(fetch=fetch,reference_year=2026)
        self.assertEqual(official.get("NQZ26@XCME")[0],date(2026,12,18))
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[0].endswith(".json"))
        self.assertTrue(calls[1].endswith("/209"))

    def test_cme_403_does_not_try_other_route(self):
        class Forbidden(Exception):
            code=403
        urls=[]
        def fetch(url):
            urls.append(url)
            raise Forbidden()
        day,diagnostic=OfficialExpiry(
            fetch=fetch,reference_year=2026).get("NQZ26@XCME")
        self.assertIsNone(day)
        self.assertEqual(len(urls),1)
        self.assertIn("HTTP 403",diagnostic)

    def test_cme_calendar_unavailable_is_explicit_and_never_guessed(self):
        def fail(_):
            raise ConnectionError("network")
        official=OfficialExpiry(fetch=fail,reference_year=2026)
        d,reason=official.get("NQZ26@XCME")
        self.assertIsNone(d)
        self.assertIn("недоступен",reason)
        d,reason=official.get("NGZ26@XNYM")
        self.assertIsNone(d)
        self.assertIn("недоступен",reason)
        d,reason=official.get("PLF27@XNYM")
        self.assertIsNone(d)
        self.assertIn("не настроен",reason)
        d,reason=official.get("SIZ26@RTSX")
        self.assertIsNone(d)

    def test_finam_valid_date_wins_over_exchange_fallback(self):
        class Good:
            def asset_details(self,_):
                return {"future_details":{"expiration_date":"2026-12-20"}}
        class Official:
            calls=0
            def get(self,symbol):
                self.calls+=1
                return date(2026,12,18),"CME NQ"
        official=Official()
        result=resolve_contract(US,[asset("NQZ26@XCME"),
                                    asset("NQH27@XCME",expires=date(2027,3,19))],
                                Good(),today=date(2026,11,13),now=NOW,
                                official=official)
        self.assertEqual(result.item.symbol,"NQZ26@XCME")
        self.assertEqual(official.calls,0)
        self.assertIn("Finam GetAsset",result.reason)

    def test_real_finim_http_error_is_sanitized_and_cme_fallback_used(self):
        class Official:
            def get(self,symbol):
                return (date(2026,12,18),"CME NQ last trade")
        api=_NoExpiryAPI(error=_HTTP(404))
        rows=[asset("NQZ26@XCME"),asset("NQH27@XCME",
                                        expires=date(2027,3,19))]
        result=resolve_contract(US,rows,api,today=NOW.date(),now=NOW,
                                official=Official())
        self.assertEqual(result.item.symbol,"NQZ26@XCME")
        self.assertIn("CME NQ",result.reason)
        self.assertNotIn("secret",result.reason)
        self.assertEqual(len(api.calls),1)

    def test_diagnostics_show_http_error_and_avoid_secret(self):
        rows=[asset("NQZ26@XCME"),asset("NQH27@XCME",
                                        expires=date(2027,3,19))]
        class FailingExchange:
            def get(self,symbol):
                return None,"CME: HTTP 403"
        result=resolve_contract(US,rows,_NoExpiryAPI(error=_HTTP(403)),
                                today=NOW.date(),now=NOW,official=FailingExchange())
        self.assertTrue(result.item.verified)
        self.assertIn("Finam GetAsset: HTTP 403",result.item.source)
        self.assertIn("CME: HTTP 403",result.item.source)
        self.assertNotIn("secret",result.item.source)
        self.assertIn("автопереход приостановлен",result.item.source)

    def test_exact_active_unknown_expiry_kept_with_diagnostic(self):
        result=resolve_contract(US,[asset("NQZ26@XCME")],
                _NoExpiryAPI(error=_HTTP(429)),today=NOW.date(),now=NOW)
        self.assertTrue(result.item.verified)
        self.assertEqual(result.item.symbol,"NQZ26@XCME")
        self.assertIn("HTTP 429",result.item.source)


if __name__=="__main__":
    unittest.main()
