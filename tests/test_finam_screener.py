"""Read-only, five-TF FVG signal tests with no network or credentials."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flowdesk.finam_screener import (
    LABELS, MIN_BARS, ScanResult, quote_last, unknown,
    select_working_fvg_signals, scan_one,
)
from flowdesk.market_data import Candle


def make_main():
    ts=datetime(2026,11,13,12,tzinfo=timezone.utc)
    sources={}
    for tf in MIN_BARS:
        bar=Candle(ts-timedelta(days=40),10,11,9,10,20)
        # Fake source has the same published bar count as the native chart.
        source=SimpleNamespace(candles=[bar]*MIN_BARS[tf],
                               confirmed_bars=MIN_BARS[tf],timeframe=tf,
                               flows=[],asof=ts)
        sources[tf]=source
    main=sources["H1"]
    main.summary_sources=sources
    return main


class TestSignalLogic(unittest.TestCase):
    def test_badges_use_only_single_confirmed_working_guard_per_tf(self):
        main=make_main()
        zones={
            "H1":SimpleNamespace(side=1,bottom=90,top=110),
            "H4":SimpleNamespace(side=-1,bottom=95,top=105),
            "D1":None,
            "W1":SimpleNamespace(side=1,bottom=130,top=140),
            "MN1":None,
        }
        def choose(source,*unused):
            return zones[source.timeframe]
        with patch("flowdesk.finam_screener.working_zone",side_effect=choose):
            result=select_working_fvg_signals(main,100,symbol="SBER@MISX")
        self.assertEqual(result.directions,(1,-1,0,0,0))
        self.assertEqual(result.as_map(),{"H1":1,"H4":-1,"D":0,"W":0,"M":0})
        self.assertTrue(result.matched(("H4",)))
        self.assertTrue(result.matched(("W","H1")))
        self.assertFalse(result.matched(("W","D")))
        self.assertFalse(result.missing(("H1","H4")))
        self.assertEqual(result.symbol,"SBER@MISX")

    def test_zone_boundaries_included_and_broken_zone_no_fallback(self):
        main=make_main()
        active=SimpleNamespace(side=1,bottom=100,top=110)
        with patch("flowdesk.finam_screener.working_zone",
                   side_effect=lambda source,*unused:
                   active if source.timeframe=="H1" else None):
            self.assertEqual(select_working_fvg_signals(main,100).directions[0],1)
            self.assertEqual(select_working_fvg_signals(main,110).directions[0],1)
            self.assertEqual(select_working_fvg_signals(main,99.99).directions[0],0)
            self.assertEqual(select_working_fvg_signals(main,110.01).directions[0],0)
        with patch("flowdesk.finam_screener.working_zone",return_value=None):
            self.assertEqual(select_working_fvg_signals(main,105).directions,
                             (0,0,0,0,0))

    def test_short_history_is_unknown_not_negative_signal(self):
        main=make_main()
        main.summary_sources["W1"].confirmed_bars=1
        main.summary_sources["MN1"].candles=[]
        with patch("flowdesk.finam_screener.working_zone",return_value=None):
            result=select_working_fvg_signals(main,105)
        self.assertEqual(result.directions,(0,0,0,None,None))
        self.assertTrue(result.missing(("W",)))
        self.assertFalse(result.missing(("H1",)))
        self.assertFalse(result.matched(("H1","M")))
        self.assertEqual(unknown("A@RTSX").directions,(None,)*5)

    def test_parser_never_confuses_bid_offer_and_close_with_last(self):
        self.assertEqual(quote_last({"quote":[{"last":{"value":"102.5"}}]}),102.5)
        self.assertEqual(quote_last({"last":"123"}),123.0)
        self.assertIsNone(quote_last({"bid":"15","ask":"17","close":"16"}))
        self.assertIsNone(quote_last({"last":"nan"}))
        self.assertIsNone(quote_last({"last":"-4"}))
        self.assertIsNone(quote_last({"quote":[]}))

    def test_scanner_downloads_only_finam_and_caches_native_frames(self):
        now=datetime(2026,11,13,16,tzinfo=timezone.utc)
        class FakeApi:
            def __init__(self):self.calls=[]
            def bars(self,symbol,tf):
                self.calls.append((symbol,tf))
                t=now-timedelta(minutes=5)
                return [Candle(t,99,101,98,100,30)]
            def latest_quote(self,symbol):
                self.calls.append((symbol,"quote"))
                return {"quote":[{"last":{"value":"100"}}]}
            def market_delay_minutes(self,mic):return 0
        class FakeService:
            def __init__(self):
                from threading import RLock
                self.api=FakeApi()
                self.clock=lambda:now
                self.lock=RLock()
                self.data={}
                self.scanner_last_pull={}
            def _series(self,symbol):return dict(self.data)
            def _upsert(self,symbol,tf,bars):self.data[tf]=bars
        service=FakeService()
        with patch("flowdesk.finam_screener.analyze_timeframes",
                   return_value=(make_main(),{})):
            with patch("flowdesk.finam_screener.select_working_fvg_signals",
                       return_value=ScanResult("SBER@MISX",(1,0,0,0,0))):
                result=scan_one(service,"SBER","MISX",object(),
                                time_sleep=lambda _:None)
                again=scan_one(service,"SBER","MISX",object(),
                               time_sleep=lambda _:None)
        self.assertEqual(result.directions[0],1)
        self.assertEqual(result.price,100)
        requested=[tf for _,tf in service.api.calls if tf!="quote"]
        self.assertEqual(set(requested),{"H1","H4","D1","W1","MN1","M1"})
        self.assertEqual(len(requested),6)
        self.assertEqual(again.price_origin,"finam_last")
        self.assertNotIn("M5",requested)


if __name__=="__main__":
    unittest.main()
