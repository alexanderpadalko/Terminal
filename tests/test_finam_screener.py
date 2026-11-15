"""Read-only, five-TF FVG signal tests with no network or credentials."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flowdesk.finam_screener import (
    LABELS, TIMEFRAMES, ScanResult, quote_last, quote_age_problem, unknown,
    select_working_fvg_signals, scan_one, FinamScreenerWorker,
)
from flowdesk.market_data import Candle
from flowdesk.engine import FlowState, FVG, analyze


def make_main(count=6):
    ts=datetime(2026,11,13,12,tzinfo=timezone.utc)
    sources={}
    steps={"H1":timedelta(hours=1),"H4":timedelta(hours=4),
           "D1":timedelta(days=1),"W1":timedelta(days=7),"MN1":timedelta(days=31)}
    for tf in TIMEFRAMES:
        step=steps[tf]
        bars=[Candle(ts-step*(count-i),100,120,80,100,20,
                     scheduled_close=ts-step*(count-i-1)) for i in range(count)]
        guard=FVG(0,1,2,110,90,1,source_tf=tf)
        source=SimpleNamespace(candles=bars,confirmed_bars=count,timeframe=tf,
            flows=[FlowState(i,side=1,lo=80,hi=120,start=0,guard=guard)
                   for i in range(count)],asof=ts)
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

    def test_uninitialized_flow_and_absent_history_are_unknown_not_negative_signal(self):
        main=make_main()
        main.summary_sources["W1"].flows=[FlowState(i) for i in range(6)]
        main.summary_sources["MN1"].candles=[]
        with patch("flowdesk.finam_screener.working_zone",return_value=None):
            result=select_working_fvg_signals(main,105)
        self.assertEqual(result.directions,(0,0,0,None,None))
        self.assertTrue(result.missing(("W",)))
        self.assertFalse(result.missing(("H1",)))
        self.assertFalse(result.matched(("H1","M")))
        self.assertEqual(unknown("A@RTSX").directions,(None,)*5)

    def test_short_ready_history_can_signal_on_every_timeframe_without_bar_floor(self):
        # The genuine selected-guard/publication path, not a mocked zone:
        # four closed candles are below every former 70/50/45/35/30 floor.
        main=make_main(count=4)
        result=select_working_fvg_signals(main,100,symbol="SIZ6@RTSX")
        self.assertEqual(result.directions,(1,1,1,1,1))
        self.assertEqual(result.missing_reasons,("",)*5)

    def test_real_uninitialized_monthly_history_never_fabricates_signal(self):
        main=make_main()
        ts=main.asof
        # One closed month and one forming month, like the observed Brent
        # archive. Run the actual pinned engine for this source.
        bars=[Candle(ts-timedelta(days=40),100,110,90,101,20,
                     scheduled_close=ts-timedelta(days=10)),
              Candle(ts-timedelta(days=10),101,111,91,102,20,
                     scheduled_close=ts+timedelta(days=20))]
        main.summary_sources["MN1"]=analyze(bars,"MN1",asof=ts)
        result=select_working_fvg_signals(main,100)
        self.assertIsNone(result.as_map()["M"])
        self.assertEqual(result.reason_map()["M"],"поток ещё не сформирован")

    def test_forming_month_without_any_closed_state_stays_unknown(self):
        main=make_main()
        ts=main.asof
        bars=[Candle(ts-timedelta(days=10),100,110,90,101,20,
                     scheduled_close=ts+timedelta(days=20))]
        main.summary_sources["MN1"]=analyze(bars,"MN1",asof=ts)
        result=select_working_fvg_signals(main,100)
        self.assertIsNone(result.as_map()["M"])
        self.assertEqual(result.reason_map()["M"],"нет подтверждённого состояния")

    def test_unready_state_and_missing_published_state_have_explicit_reasons(self):
        main=make_main()
        main.summary_sources["W1"].flows=[FlowState(i) for i in range(6)]
        main.summary_sources["MN1"].flows=[]
        with patch("flowdesk.finam_screener.working_zone",return_value=None):
            result=select_working_fvg_signals(
                main,100,symbol="SIZ26@RTSX",
                fetch_problems={"W1":"Finam не вернул свечи"})
        self.assertEqual(result.directions,(0,0,0,None,None))
        self.assertEqual(result.reason_map()["W"],
                         "поток ещё не сформирован; Finam не вернул свечи")
        self.assertEqual(result.reason_map()["M"],"нет подтверждённого состояния")
        self.assertTrue(result.missing(("W","M")))
        self.assertFalse(result.matched(("W","M")))
        self.assertEqual(result.reason_map()["H1"],"")

    def test_missing_sources_and_access_denial_have_distinct_safe_messages(self):
        main=make_main()
        main.summary_sources.pop("MN1")
        main.summary_sources.pop("W1")
        with patch("flowdesk.finam_screener.working_zone",return_value=None):
            result=select_working_fvg_signals(
                main,100,fetch_problems={
                    "MN1":"нет доступа Finam (403)",
                    "W1":"Finam не вернул свечи",
                })
        self.assertEqual(result.directions[-2:],(None,None))
        self.assertEqual(result.reason_map()["M"],"нет свечей; нет доступа Finam (403)")
        self.assertEqual(result.reason_map()["W"],"нет свечей; Finam не вернул свечи")
        self.assertEqual(unknown("NQZ26@XCME","rate_limited").reason_map()["M"],
                         "лимит запросов Finam")
        self.assertNotIn("secret",result.reason_map()["W"])

    def test_parser_never_confuses_bid_offer_and_close_with_last(self):
        self.assertEqual(quote_last({"quote":[{"last":{"value":"102.5"}}]}),102.5)
        self.assertEqual(quote_last({"last":"123"}),123.0)
        self.assertIsNone(quote_last({"bid":"15","ask":"17","close":"16"}))
        self.assertIsNone(quote_last({"last":"nan"}))
        self.assertIsNone(quote_last({"last":"-4"}))
        self.assertIsNone(quote_last({"quote":[]}))

    def test_exact_quote_takes_precedence_over_unlabelled_batch_member(self):
        rows={"quote":[{"last":{"value":"500"}},
                       {"symbol":"SBER@MISX","last":{"value":"100"}}]}
        self.assertEqual(quote_last(rows,"SBER@MISX"),100)
        self.assertEqual(quote_last({"quote":[{"last":500}]},"SBER@MISX"),500)
        self.assertIsNone(quote_last({"quote":[{"symbol":"GAZP@MISX","last":99}]},"SBER@MISX"))

    def test_timestamped_quote_rejects_stale_and_future_transactions(self):
        observed=datetime(2026,11,13,16,tzinfo=timezone.utc)
        def payload(stamp):
            return {"quote":[{"symbol":"SBER@MISX","last":100,"timestamp":stamp}]}
        self.assertEqual(quote_age_problem(payload("2026-11-13T15:59:00Z"),
                                           observed,"SBER@MISX"),"")
        # A previous-session LAST is acceptable for an analytical screener,
        # including on weekends. Seven days is the inclusive limit.
        self.assertEqual(quote_age_problem(
            payload("2026-11-12T10:00:00Z"),observed,"SBER@MISX"),"")
        self.assertEqual(quote_age_problem(
            payload("2026-11-06T16:00:00Z"),observed,"SBER@MISX"),"")
        self.assertIn("устарела",quote_age_problem(
            payload("2026-11-06T15:59:59Z"),observed,"SBER@MISX"))
        self.assertIn("опережает",quote_age_problem(
            payload("2026-11-13T17:00:00Z"),observed,"SBER@MISX"))
        self.assertEqual(quote_age_problem({"symbol":"SBER@MISX","last":100},
                                           observed,"SBER@MISX"),"")

    def test_quote_age_checks_the_same_single_record_as_last(self):
        observed=datetime(2026,11,13,16,tzinfo=timezone.utc)
        for stamp, expected in (("2026-11-06T16:00:00Z", ""),
                                ("2026-11-06T15:59:59Z", "устарела"),
                                ("2026-11-13T17:00:00Z", "опережает")):
            quote={"last":100,"timestamp":stamp}
            for payload in (quote, {"quote":quote}, {"quote":[quote]}):
                with self.subTest(stamp=stamp,payload=payload):
                    self.assertEqual(quote_last(payload,"SBER@MISX"),100)
                    problem=quote_age_problem(payload,observed,"SBER@MISX")
                    self.assertIn(expected,problem) if expected else self.assertEqual(problem,"")

    def test_explicit_invalid_quote_time_is_not_a_missing_timestamp(self):
        observed=datetime(2026,11,13,16,tzinfo=timezone.utc)
        for stamp in (0,False,"",{},[],"not-a-date"):
            with self.subTest(stamp=stamp):
                payload={"quote":[{"last":100,"timestamp":stamp}]}
                self.assertTrue(quote_age_problem(payload,observed,"SBER@MISX"))
        self.assertEqual(quote_age_problem({"quote":[{"last":100}]},
                                           observed,"SBER@MISX"),"")

    def test_quote_age_ignores_other_symbols_in_batch(self):
        observed=datetime(2026,11,13,16,tzinfo=timezone.utc)
        payload={"quote":[{"symbol":"GAZP@MISX","last":10,
                            "timestamp":"2000-01-01T00:00:00Z"},
                           {"symbol":"sber@misx","last":100,
                            "timestamp":"2026-11-13T15:59:00Z"}]}
        self.assertEqual(quote_last(payload,"SBER@MISX"),100)
        self.assertEqual(quote_age_problem(payload,observed,"SBER@MISX"),"")

    def test_monthly_backfill_migrates_five_year_boundary_and_checks_old_window(self):
        from flowdesk.finam_api import FinamAPI, DEPTH_DAYS
        from flowdesk.finam_market import FinamMarketService
        from flowdesk.finam_screener import _backfill_higher_history
        from pathlib import Path
        import tempfile
        now=datetime(2026,11,14,12,tzinfo=timezone.utc)
        boundary=now-timedelta(days=DEPTH_DAYS["MN1"])
        class Archive(FinamAPI):
            def __init__(self):self.calls=[]
            def bars(self,symbol,tf,*,start=None,end=None):
                self.calls.append((symbol,tf,start,end))
                return [Candle(datetime(2000,5,1,tzinfo=timezone.utc),
                               100,101,99,100,5)] if tf=="MN1" else []
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/"finam.sqlite",api=Archive(),
                                      clock=lambda:now)
            market._upsert("SBER@MISX","MN1",[
                Candle(boundary,100,101,99,100,5)])
            market.save_history_progress("SBER@MISX","MN1",{
                "cursor":boundary,"exhausted":True})
            _backfill_higher_history(market,"SBER@MISX",now,
                                     market._series("SBER@MISX"))
            self.assertTrue(any(tf=="MN1" and start<boundary
                                for _,tf,start,end in market.api.calls))
            self.assertIn(datetime(2000,5,1,tzinfo=timezone.utc),
                {bar.time for bar in market._series("SBER@MISX")["MN1"]})
            self.assertNotIn("GAZP@MISX",{symbol for symbol,_,_,_ in market.api.calls})

    def test_rate_limit_explicitly_marks_remaining_not_yet_checked_rows(self):
        from flowdesk.finam_api import FinamAPIError
        items=[SimpleNamespace(symbol=f"TEST{i}@MISX",verified=True)
               for i in range(4)]
        results=[]
        calls=[]
        def evaluate(_service,ticker,mic,_settings,**kwargs):
            calls.append(ticker)
            if ticker=="TEST1":
                raise FinamAPIError("stop",http_status=429)
            return ScanResult(ticker+"@"+mic,(0,0,0,0,0))
        worker=FinamScreenerWorker(object(),items,object(),evaluate=evaluate,max_workers=1)
        worker.row.connect(results.append)
        worker.run()
        self.assertEqual(calls,["TEST0","TEST1"])
        self.assertEqual(len(results),4)
        self.assertEqual(results[0].directions,(0,0,0,0,0))
        for result in results[1:]:
            self.assertEqual(result.error_code,"rate_limited")
            self.assertEqual(result.missing_reasons[4],"лимит запросов Finam")

    def test_bounded_history_backfill_resumes_and_persists_without_contract_stitching(self):
        from flowdesk.finam_api import FinamAPI
        from flowdesk.finam_market import FinamMarketService
        from flowdesk.finam_screener import _backfill_higher_history
        from pathlib import Path
        import tempfile
        from threading import RLock
        now=datetime(2026,11,13,16,tzinfo=timezone.utc)
        symbol="SIZ6@RTSX"
        class HistoryAPI(FinamAPI):
            def __init__(self):
                self.calls=[]
            def bars(self,name,tf,*,start=None,end=None,**unused):
                self.calls.append((name,tf,start,end))
                step=timedelta(days=7 if tf=="W1" else 30)
                if tf not in ("W1","MN1") or start is None:
                    return []
                return [Candle(start+step*i,99,101,98,100,30)
                        for i in range(1,9) if start+step*i<end]
        with tempfile.TemporaryDirectory() as folder:
            service=FinamMarketService(Path(folder)/"finam.sqlite3",
                                       api=HistoryAPI(),clock=lambda:now)
            recent=[
                Candle(now-timedelta(days=i*7),99,101,98,100,30)
                for i in range(1,6)]
            service._upsert(symbol,"W1",recent)
            service._upsert(symbol,"MN1",[
                Candle(now-timedelta(days=i*30),99,101,98,100,30)
                for i in range(1,4)])
            first=_backfill_higher_history(
                service,symbol,now,service._series(symbol),
                time_sleep=lambda delay:None)
            self.assertIn("W1",first)
            self.assertGreater(len(service._series(symbol)["W1"]),5)
            initial_calls=list(service.api.calls)
            self.assertTrue(initial_calls)
            self.assertTrue(all(row[0]==symbol and row[2]<row[3]
                                for row in initial_calls))
            second=_backfill_higher_history(
                service,symbol,now,service._series(symbol),
                time_sleep=lambda delay:None)
            self.assertGreaterEqual(len(service._series(symbol)["W1"]),35)
            previous={(tf,start,end) for _,tf,start,end in initial_calls}
            self.assertTrue(all((tf,start,end) not in previous
                                for _,tf,start,end in service.api.calls[len(initial_calls):]))
            self.assertTrue(all(name==symbol for name,_,_,_ in service.api.calls))
            self.assertIn("MN1",service._series(symbol))

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
        import tempfile
        from pathlib import Path
        from flowdesk.finam_market import FinamMarketService
        from flowdesk.settings import IndicatorSettings
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service=FinamMarketService(Path(temp.name)/"cache.sqlite3",api=FakeApi(),clock=lambda:now)
        with patch("flowdesk.finam_market.analyze_timeframes",
                   return_value=(make_main(),{})):
            with patch("flowdesk.finam_screener.select_working_fvg_signals",
                       return_value=ScanResult("SBER@MISX",(1,0,0,0,0))):
                result=scan_one(service,"SBER","MISX",IndicatorSettings(),
                                time_sleep=lambda _:None)
                again=scan_one(service,"SBER","MISX",IndicatorSettings(),
                               time_sleep=lambda _:None)
        self.assertEqual(result.directions[0],1)
        self.assertEqual(result.price,100)
        requested=[tf for _,tf in service.api.calls if tf!="quote"]
        self.assertEqual(set(requested),{"H1","H4","D1","W1","MN1"})
        self.assertEqual(len(requested),5)
        self.assertEqual(again.price_origin,"finam_last")
        self.assertNotIn("M5",requested)


if __name__=="__main__":
    unittest.main()
