"""Concurrency, freshness, incremental recovery and Finam-only expiry regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone,date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

from flowdesk.finam_requests import RequestCoordinator
from flowdesk.finam_api import FinamAPI,FinamAPIError
from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_stream import FinamEvent
from flowdesk.finam_screener import FinamScreenerWorker,ScanResult,scan_one
from flowdesk.finam_rotation import expiry_date,resolve_futures
from flowdesk.market_data import Candle
from flowdesk.settings import IndicatorSettings

NOW=datetime(2026,11,14,12,30,tzinfo=timezone.utc)

class Feed:
    def __init__(self):
        self.calls=[]
        self.rows=[Candle(NOW.replace(minute=0)-timedelta(hours=i),100,102,99,101,5)
                   for i in reversed(range(5))]
    def market_delay_minutes(self,mic):return 0
    def bars(self,symbol,tf,**kwargs):
        self.calls.append((symbol,tf,kwargs))
        return list(self.rows)
    def latest_quote(self,symbol):
        self.calls.append((symbol,"quote",{}))
        return {"symbol":symbol,"last":{"value":"101"}}

class TestRequests(unittest.TestCase):
    def test_rate_budget_and_bounded_429_retry_use_one_shared_clock(self):
        current=[0.0]
        scheduler=RequestCoordinator(per_minute=2,clock=lambda:current[0])
        scheduler.condition.wait=lambda delay:current.__setitem__(0,current[0]+max(delay,.001))
        starts=[]
        for i in range(3):scheduler.run(i,lambda:starts.append(current[0]))
        self.assertEqual(starts,[0.0,30.0,60.0])
        attempts=[]
        def limited():
            attempts.append(current[0])
            raise FinamAPIError("limited",http_status=429,retry_after=60)
        with self.assertRaises(FinamAPIError):scheduler.run("limited",limited)
        self.assertEqual(len(attempts),2)
        self.assertGreaterEqual(attempts[1]-attempts[0],60)

    def test_chart_priority_and_cancellation_while_waiting(self):
        scheduler=RequestCoordinator(per_minute=1000000,concurrency=1)
        entered=threading.Event();release=threading.Event();cancel=threading.Event()
        order=[]
        def held():
            entered.set();self.assertTrue(release.wait(3))
        def request(name,priority,stopped=lambda:False):
            with scheduler.scope(priority=priority,cancelled=stopped):
                return scheduler.run(name,lambda:order.append(name))
        with ThreadPoolExecutor(max_workers=4) as pool:
            owner=pool.submit(scheduler.run,"held",held)
            self.assertTrue(entered.wait(2))
            background=pool.submit(request,"background",10)
            chart=pool.submit(request,"chart",0)
            cancelled=pool.submit(request,"cancelled",10,cancel.is_set)
            deadline=time.monotonic()+2
            while len(scheduler.waiting)<3 and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(len(scheduler.waiting),3)
            cancel.set()
            with self.assertRaises(InterruptedError):cancelled.result(timeout=1)
            release.set();owner.result(timeout=2);chart.result(timeout=2);background.result(timeout=2)
        self.assertEqual(order,["chart","background"])

    def test_identical_inflight_requests_are_sent_once(self):
        scheduler=RequestCoordinator(per_minute=1000000)
        entered=threading.Event();release=threading.Event();calls=[]
        def send():
            calls.append(1);entered.set();self.assertTrue(release.wait(3));return {"ok":True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(scheduler.run,"same",send)
            self.assertTrue(entered.wait(2))
            second=pool.submit(scheduler.run,"same",send)
            time.sleep(.03);release.set()
            self.assertEqual(first.result(timeout=2),second.result(timeout=2))
        self.assertEqual(len(calls),1)

class TestSharedHistory(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"finam.sqlite3"
        self.now=NOW;self.api=Feed()
        self.service=FinamMarketService(self.path,api=self.api,clock=lambda:self.now)
        self.symbol="SIZ6@RTSX"

    def test_incremental_overlap_and_restart_reuse_do_not_trim_archive(self):
        old=Candle(NOW-timedelta(days=300),1,2,1,2,3)
        self.service._upsert(self.symbol,"H1",[old,*self.api.rows])
        self.service.refresh_series(self.symbol,["H1"])
        kwargs=self.api.calls[-1][2]
        self.assertEqual(kwargs["start"],self.api.rows[-3].time)
        self.assertIn(old,self.service._series(self.symbol)["H1"])
        restarted=FinamMarketService(self.path,api=self.api,clock=lambda:self.now)
        restarted.refresh_series(self.symbol,["H1"])
        self.assertEqual(len(self.api.calls),1)
        self.now+=timedelta(days=1,seconds=1)
        restarted.refresh_series(self.symbol,["H1"])
        self.assertEqual(self.api.calls[-1][2],{})

    def test_same_tf_refresh_deduplicates_without_locking_other_database_writes(self):
        entered=threading.Event();release=threading.Event()
        original=self.api.bars
        def blocked(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(3));return original(*args,**kwargs)
        self.api.bars=blocked
        with ThreadPoolExecutor(max_workers=3) as pool:
            first=pool.submit(self.service.refresh_series,self.symbol,["H1"])
            self.assertTrue(entered.wait(2))
            second=pool.submit(self.service.refresh_series,self.symbol,["H1"])
            write=pool.submit(self.service._upsert,"EUZ6@RTSX","H1",self.api.rows)
            self.assertTrue(write.result(timeout=1))
            release.set();first.result(timeout=2);second.result(timeout=2)
        self.assertEqual(len(self.api.calls),1)

    def test_stream_health_is_per_frame_and_reconnect_forces_recovery(self):
        self.service.refresh_series(self.symbol,["H1"])
        self.now+=timedelta(seconds=301)
        event=FinamEvent("candles","SIZ6","RTSX",1,self.now,"H1",tuple(self.api.rows))
        self.service.ingest_stream("SIZ6","RTSX",[event])
        self.service.refresh_series(self.symbol,["H1"])
        self.assertEqual(len(self.api.calls),1)
        self.service.refresh_series(self.symbol,["M1"])
        self.assertEqual(len(self.api.calls),2)
        self.service.mark_stream_reconnect(self.symbol)
        self.service.refresh_series(self.symbol,["H1"])
        self.assertEqual(len(self.api.calls),3)
        self.assertNotIn((self.symbol,"H1"),self.service._repair)

    def test_old_snapshot_does_not_suppress_http_and_gap_is_repaired(self):
        self.service.refresh_series(self.symbol,["H1"])
        self.now+=timedelta(hours=3)
        event=FinamEvent("candles","SIZ6","RTSX",1,self.now,"H1",tuple(self.api.rows))
        self.service.ingest_stream("SIZ6","RTSX",[event])
        self.service.refresh_series(self.symbol,["H1"])
        self.assertEqual(len(self.api.calls),2)
        later=Candle(self.now.replace(minute=0),100,103,99,102,5)
        self.service.ingest_stream("SIZ6","RTSX",[
            FinamEvent("candles","SIZ6","RTSX",1,self.now,"H1",(later,))])
        self.assertIn((self.symbol,"H1"),self.service._repair)
        self.service.refresh_series(self.symbol,["H1"])
        self.assertEqual(len(self.api.calls),3)

    def test_calculations_reused_but_corrections_settings_and_close_invalidate(self):
        from flowdesk.pipeline import analyze_timeframes
        settings=IndicatorSettings();bars={"H1":self.api.rows}
        with patch("flowdesk.finam_market.analyze_timeframes",wraps=analyze_timeframes) as calculate:
            first=self.service.analyze_cached(self.symbol,"H1",bars,settings,self.now)
            again=self.service.analyze_cached(self.symbol,"H1",bars,settings,self.now+timedelta(seconds=5))
            self.assertIs(first,again);self.assertEqual(calculate.call_count,1)
            self.service.analyze_cached(self.symbol,"H1",bars,settings,self.now+timedelta(hours=1))
            self.assertEqual(calculate.call_count,2)
            corrected=list(self.api.rows);b=corrected[1]
            corrected[1]=Candle(b.time,b.open,b.high+1,b.low,b.close,b.volume)
            self.service.analyze_cached(self.symbol,"H1",{"H1":corrected},settings,self.now)
            self.assertEqual(calculate.call_count,3)
            changed=settings.changed(fvgExcludeGaps=not settings.fvgExcludeGaps)
            self.service.analyze_cached(self.symbol,"H1",{"H1":corrected},changed,self.now)
            self.assertEqual(calculate.call_count,4)

    def test_empty_or_denied_frames_do_not_create_a_tight_request_loop(self):
        self.api.bars=lambda symbol,tf,**kwargs:[]
        self.service.refresh_series(self.symbol,["H1"])
        def forbidden(*args,**kwargs):raise AssertionError("duplicate empty-frame fetch")
        self.api.bars=forbidden
        self.assertIn("H1",self.service.refresh_series(self.symbol,["H1"]))
        self.service.invalidate_network_cache()
        calls=[]
        def denied(*args,**kwargs):
            calls.append(1);raise FinamAPIError("denied",http_status=403)
        self.api.bars=denied
        self.service.refresh_series(self.symbol,["H1"])
        self.service.refresh_series(self.symbol,["H1"])
        self.assertEqual(len(calls),1)

    def test_history_backfill_cursor_survives_restart(self):
        saved={"cursor":NOW-timedelta(days=365),"exhausted":True}
        self.service.save_history_progress(self.symbol,"MN1",saved)
        restarted=FinamMarketService(self.path,api=self.api,clock=lambda:self.now)
        self.assertEqual(restarted.load_history_progress()[(self.symbol,"MN1")],saved)

    def test_scanner_only_fetches_m1_when_last_quote_is_absent(self):
        scan_one(self.service,"SIZ6","RTSX",IndicatorSettings())
        self.assertNotIn("M1",[tf for _,tf,_ in self.api.calls])
        self.service.invalidate_network_cache()
        self.api.latest_quote=lambda symbol:{"symbol":symbol}
        scan_one(self.service,"SIZ6","RTSX",IndicatorSettings())
        self.assertIn("M1",[tf for _,tf,_ in self.api.calls])

class TestParallelScanner(unittest.TestCase):
    def test_cancelled_pass_closes_its_background_executors(self):
        entered=threading.Event()
        def evaluate(service,ticker,mic,settings,*,cancelled):
            entered.set()
            while not cancelled():time.sleep(.005)
            raise InterruptedError()
        worker=FinamScreenerWorker(object(),[
            SimpleNamespace(symbol=f"T{i}@RTSX",verified=True) for i in range(6)],
            object(),evaluate=evaluate,backfill=False)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
        finally:
            worker.requestInterruption()
            self.assertTrue(worker.wait(2000))
        self.assertFalse(any(t.name.startswith("FinamScan") for t in threading.enumerate()))

    def test_three_instruments_run_concurrently_and_publish_progress(self):
        barrier=threading.Barrier(3);lock=threading.Lock();active=[0,0]
        def evaluate(service,ticker,mic,settings,**kwargs):
            with lock:
                active[0]+=1;active[1]=max(active)
            if int(ticker[1:])<3:barrier.wait(timeout=3)
            with lock:active[0]-=1
            return ScanResult(ticker+"@"+mic,(0,)*5)
        items=[SimpleNamespace(symbol=f"T{i}@RTSX",verified=True) for i in range(5)]
        rows=[];progress=[]
        worker=FinamScreenerWorker(object(),items,object(),evaluate=evaluate,backfill=False)
        worker.row.connect(rows.append);worker.progress.connect(lambda a,b:progress.append((a,b)))
        worker.run()
        self.assertEqual(active[1],3);self.assertEqual(len(rows),5)
        self.assertEqual(progress,[(i,5) for i in range(6)])

class TestFinamOnlyExpiry(unittest.TestCase):
    def test_startup_reads_only_selected_card_until_its_roll_day(self):
        from test_moex_startup_roll import row, NQ_ROLLOVER_CONTRACT
        from flowdesk.finam_rotation import resolve_contract
        calls=[]
        class API:
            def asset_details(self,symbol):
                calls.append(symbol)
                return {"expiration_date":{"year":2026,"month":12,"day":18}}
        rows=[row("NQZ6","XCME"),row("NQH7","XCME"),row("NQM7","XCME")]
        result=resolve_contract(NQ_ROLLOVER_CONTRACT,rows,API(),today=NOW.date(),now=NOW)
        self.assertEqual(result.item.symbol,"NQZ6@XCME")
        self.assertEqual(calls,["NQZ6@XCME"])

    def test_brent_contract_year_is_distinct_from_expiration_year(self):
        from test_moex_startup_roll import row,API
        from flowdesk.finam_universe import US_CONTRACTS
        from flowdesk.finam_rotation import resolve_contract
        contract=next(c for c in US_CONTRACTS if c.display=="BRNF2027")
        rows=[row("BRNF27","IFEU","2026-11-30")]
        result=resolve_contract(contract,rows,API(rows),today=NOW.date(),now=NOW)
        self.assertEqual(result.item.label,"BRNF2027")
        self.assertEqual(result.item.symbol,"BRNF27@IFEU")

    def test_mixed_case_cards_use_exact_api_case_and_account_retry(self):
        paths=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return json.dumps({"expiration_date":{"year":2026,"month":12,"day":17}}).encode()
        def opener(request,timeout):
            paths.append(request.full_url)
            if "account_id=" not in request.full_url:
                raise HTTPError(request.full_url,400,"account required",{},None)
            return Response()
        api=FinamAPI(opener=opener,coordinator=RequestCoordinator(per_minute=1000000))
        api._jwt="test-access";api._expiry=float("inf");api._account_ids=["test-account"]
        for symbol in ("SiZ6@RTSX","EuZ6@RTSX"):
            self.assertEqual(expiry_date(api.asset_details(symbol),mic="RTSX"),date(2026,12,17))
        self.assertEqual(len(paths),4)
        self.assertIn("SiZ6%40RTSX",paths[0]);self.assertIn("EuZ6%40RTSX",paths[2])

    def test_calendar_date_wins_over_midnight_timestamp(self):
        card={"expiration_date":{"year":2026,"month":12,"day":18},
              "future_details":{"expiration_date":"2026-12-18T00:00:00Z"}}
        self.assertEqual(expiry_date(card,mic="XCME"),date(2026,12,18))
        self.assertEqual(expiry_date({"asset_details":card},mic="XCME"),date(2026,12,18))

    def test_missing_finam_dates_keep_exact_contract_and_block_roll(self):
        from test_moex_startup_roll import row,API
        rows=[row("NQZ6","XCME"),row("NQH7","XCME"),row("SiZ6","RTSX"),row("SiH7","RTSX")]
        result,_,_=resolve_futures(rows,API(rows),now=NOW)
        self.assertEqual(result["us:NQZ2026"].symbol,"NQZ6@XCME")
        self.assertEqual(result["ru:SIZ2026"].symbol,"SiZ6@RTSX")
        self.assertIn("не подтверждена",result["ru:SIZ2026"].source)
