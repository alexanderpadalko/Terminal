"""Startup refresh: MOEX top-10 over 20 sessions and date-based futures roll."""
import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
import tempfile
import unittest

from PySide6.QtWidgets import QApplication

from flowdesk.finam_universe import US_CONTRACTS, RU_CONTRACTS, Contract, US_FUTURES
from flowdesk.finam_rotation import (
    RollState, dominates, resolve_contract, resolve_futures, expiry_date,
)
from flowdesk.moex_reference import refresh_top10
from flowdesk.finam_catalog_worker import FinamCatalogWorker


NOW=datetime(2026,11,13,14,30,tzinfo=timezone.utc)
# Keep regression coverage of the NQ rollover engine independently of
# whether NQ is included in the user-visible curated universe.
NQ_ROLLOVER_CONTRACT=Contract("NQZ2026","Nasdaq 100",US_FUTURES,("NQZ2026","NQZ26","NQZ6"),("XCME",))
STAMP="2026-11-13T14:25:00Z"


def row(ticker,mic,expiry=None):
    result={"ticker":ticker,"mic":mic,"symbol":ticker+"@"+mic,"type":"TYPE_FUTURE"}
    if expiry:
        result["future_details"]={"expiration_date":expiry}
    return result


def quote(symbol,volume,turnover,stamp=STAMP):
    return {"quote":{"symbol":symbol,"timestamp":stamp,
                      "volume":{"value":str(volume)},
                      "turnover":{"value":str(turnover)}}}


class API:
    def __init__(self,rows,quotes=None):
        self.rows=rows
        self.quotes=quotes or {}
        self.calls=[]
    def active_assets(self,*,cancelled):
        return self.rows
    def asset_details(self,symbol):
        self.calls.append(("details",symbol))
        return next(r for r in self.rows if r["symbol"]==symbol)
    def latest_quote(self,symbol):
        self.calls.append(("quote",symbol))
        return self.quotes[symbol]


class TestRollover(unittest.TestCase):
    def test_contract_stays_until_day_before_expiry_without_quote_calls(self):
        base=NQ_ROLLOVER_CONTRACT
        rows=[row("NQZ6","XCME","2026-12-18"),
              row("NQH7","XCME","2027-03-19")]
        api=API(rows)
        for day,symbol in [(date(2026,12,16),"NQZ6@XCME"),
                           (date(2026,12,17),"NQH7@XCME"),
                           (date(2026,12,18),"NQH7@XCME")]:
            with self.subTest(day=day):
                result=resolve_contract(base,rows,api,now=NOW,today=day)
                self.assertEqual(result.item.symbol,symbol)
                if day==date(2026,12,17):
                    self.assertEqual(result.item.label,"NQH2027")
                    self.assertIn("за день",result.reason)
        self.assertFalse(any(kind=="quote" for kind,_ in api.calls))

    def test_next_contract_with_higher_volume_does_not_roll_early(self):
        base=NQ_ROLLOVER_CONTRACT
        rows=[row("NQZ6","XCME","2026-12-18"),
              row("NQH7","XCME","2027-03-19")]
        api=API(rows,{"NQZ6@XCME":quote("NQZ6@XCME",1,1),
                      "NQH7@XCME":quote("NQH7@XCME",999999,999999)})
        result=resolve_contract(base,rows,api,now=NOW,today=date(2026,11,13))
        self.assertEqual(result.item.symbol,"NQZ6@XCME")
        self.assertFalse(any(kind=="quote" for kind,_ in api.calls))


    def test_expired_contract_is_not_selected_even_if_quotes_unavailable(self):
        base=NQ_ROLLOVER_CONTRACT
        rows=[row("NQZ6","XCME","2026-11-12"),
              row("NQH7","XCME","2027-03-19")]
        api=API(rows)
        result=resolve_contract(base,rows,api,today=NOW.date(),now=NOW)
        self.assertEqual(result.item.symbol,"NQH7@XCME")
        self.assertTrue(result.item.verified)

    def test_unknown_expiry_keeps_exact_active_contract_but_stops_rolling(self):
        base=NQ_ROLLOVER_CONTRACT
        data=[row("NQZ6","XCME"),row("NQH7","XCME","2027-03-19")]
        result=resolve_contract(base,data,API(data),today=NOW.date(),now=NOW)
        self.assertTrue(result.item.verified)
        self.assertEqual(result.item.symbol,"NQZ6@XCME")
        self.assertIn("не подтверждена",result.item.source)
        self.assertEqual(result.state_symbol,"NQZ6@XCME")

    def test_unavailable_contract_without_confirmed_expiry_is_disabled(self):
        base=NQ_ROLLOVER_CONTRACT
        data=[row("NQH7","XCME")]
        result=resolve_contract(base,data,API(data),today=NOW.date(),now=NOW)
        self.assertFalse(result.item.verified)
        self.assertFalse(result.state_symbol)

    def test_nested_timestamp_expiry_is_exchange_local(self):
        from flowdesk.finam_rotation import _date
        # 00:30 UTC is still the previous trading day in Chicago.
        self.assertEqual(_date("2026-12-18T00:30:00Z",mic="XCME"),
                         date(2026,12,17))
        timestamp=int(datetime(2026,12,18,0,30,tzinfo=timezone.utc).timestamp())
        obj={"asset_details":{"future_details":
             {"expiration_date":{"seconds":str(timestamp),"nanos":0}}}}
        self.assertEqual(expiry_date(obj,mic="XCME"),date(2026,12,17))
        self.assertEqual(expiry_date({"expiration_date":
                         {"year":2026,"month":12,"day":18}},mic="XCME"),
                         date(2026,12,18))

    def test_no_quote_or_expiry_fabrication_when_next_date_missing(self):
        base=NQ_ROLLOVER_CONTRACT
        data=[row("NQZ6","XCME","2026-12-18"),row("NQH7","XCME")]
        result=resolve_contract(base,data,API(data),
                                today=date(2026,12,17),now=NOW)
        self.assertEqual(result.item.symbol,"NQZ6@XCME")
        self.assertIn("следующий контракт не подтверждён",result.reason)

    def test_roll_does_not_choose_mini_micro_or_other_exchange(self):
        base=NQ_ROLLOVER_CONTRACT
        data=[row("NQZ6","XCME","2026-12-18"),
              row("MNQH7","XCME","2027-03-19"),
              row("NQH7","RTSX","2027-03-19")]
        result=resolve_contract(base,data,API(data),today=NOW.date(),now=NOW)
        self.assertEqual(result.item.symbol,"NQZ6@XCME")

    def test_silver_and_ruble_futures_are_separate(self):
        us=next(c for c in US_CONTRACTS if c.display=="SIZ2026")
        ru=next(c for c in RU_CONTRACTS if c.display=="SIZ2026")
        rows=[row("SIZ6","XCEC","2026-12-29"),
              row("SIZ6","RTSX","2026-12-17")]
        self.assertEqual(resolve_contract(us,rows,API(rows),today=NOW.date(),now=NOW).item.symbol,
                         "SIZ6@XCEC")
        self.assertEqual(resolve_contract(ru,rows,API(rows),today=NOW.date(),now=NOW).item.symbol,
                         "SIZ6@RTSX")

    def test_no_backward_roll_once_previous_front_remembered(self):
        base=NQ_ROLLOVER_CONTRACT
        rows=[row("NQZ6","XCME","2026-12-18"),
              row("NQH7","XCME","2027-03-19")]
        first=resolve_contract(base,rows,API(rows),today=NOW.date(),now=NOW,
                               saved="NQH7@XCME")
        self.assertEqual(first.item.symbol,"NQH7@XCME")
        self.assertNotIn("NQZ6@XCME",first.reason)

    def test_quotes_must_belong_to_same_session_and_be_fresh(self):
        self.assertTrue(dominates(quote("NQZ6@XCME",100,1000),
            quote("NQH7@XCME",120,1400),mic="XCME",symbol="NQZ6",now=NOW))
        self.assertFalse(dominates(quote("NQZ6@XCME",100,1000,"2026-11-12T23:25:00Z"),
            quote("NQH7@XCME",120,1400),mic="XCME",symbol="NQZ6",now=NOW))
        self.assertFalse(dominates(quote("NQZ6@XCME",100,1000),
            {"quote":{"volume":{"value":"120"}}},mic="XCME",symbol="NQZ6",now=NOW))

    def test_perpetual_instrument_does_not_roll(self):
        c=next(x for x in RU_CONTRACTS if x.display=="USDRUB.P")
        result=resolve_contract(c,[row("USDRUBF","RTSX")],API([]),
                                today=NOW.date(),now=NOW)
        self.assertEqual(result.item.symbol,"USDRUBF@RTSX")
        self.assertEqual(result.reason,"perpetual")


class TestMoexTurnover(unittest.TestCase):


    def test_exact_moex_top10_refreshes_at_launch(self):
        from flowdesk.watchlist import WatchlistService
        cache=WatchlistService().cached()
        self.assertIsNotNone(cache)
        class Watchlist:
            def __init__(self):
                self.value=cache
                self.calls=0
            def cached(self):return self.value
            def due(self,value):return True
            def refresh(self,*,cancelled,stocks_only=False):
                self.stocks_only=stocks_only
                self.calls+=1
                self.value=dict(self.value,
                     window=["2026-11-10","2026-11-11","2026-11-12"])
                return self.value
        mock=Watchlist()
        stocks,status=refresh_top10(watchlist=mock)
        self.assertEqual(len(stocks),10)
        self.assertEqual(mock.calls,1)
        self.assertTrue(mock.stocks_only)
        self.assertIn("MOEX",status)
        self.assertIn("2026-11-12",status)


class TestCatalogStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_startup_refreshes_stocks_and_switches_one_day_before_expiry(self):
        from flowdesk.watchlist import WatchlistService
        base=WatchlistService().cached()
        rows=[row("GCZ6","XCME","2026-11-14"),
              row("GCH7","XCME","2027-03-19"),
              row("SiZ6","RTSX","2026-11-14"),
              row("SiH7","RTSX","2027-03-18")]
        api=API(rows)
        class StockSource:
            def __init__(self):self.calls=0
            def cached(self):return base
            def due(self,x):return True
            def refresh(self,*,cancelled,stocks_only=False):
                self.stocks_only=stocks_only
                self.calls+=1
                return base
        with tempfile.TemporaryDirectory() as directory:
            source=StockSource()
            worker=FinamCatalogWorker(api,watchlist=source,
                roll_state=RollState(Path(directory)/"state.json"),
                clock=lambda:NOW)
            messages=[]
            worker.loaded.connect(messages.append)
            worker.run()
            self.assertEqual(source.calls,1)
            self.assertTrue(source.stocks_only)
            self.assertEqual(len(messages),1)
            payload=messages[0]
            self.assertEqual(len(payload["stocks"]),10)
            self.assertTrue(payload["ranking_status"].startswith("2026"))
            self.assertEqual(payload["futures"]["us:GCZ2026"].symbol,"GCH7@XCME")
            self.assertEqual(payload["futures"]["ru:SIZ2026"].symbol,"SiH7@RTSX")
            self.assertEqual(payload["roll_map"]["GCZ6@XCME"],"GCH7@XCME")
            self.assertTrue((Path(directory)/"state.json").exists())
            self.assertFalse(any(kind=="quote" for kind,_ in api.calls))




if __name__=="__main__":
    unittest.main()
