"""The chart must bootstrap its own trading-day history without a scanner."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timedelta
from pathlib import Path
import tempfile,threading,unittest
from unittest.mock import patch

from flowdesk import moex_trading_family as moex
from flowdesk.finam_api import FinamAPI,FinamAPIError
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle,MOSCOW
from flowdesk.provider_candles import NATIVE_HIGHER
from flowdesk.settings import IndicatorSettings

NOW=datetime(2026,11,16,12,tzinfo=MOSCOW)
SYMBOL='CRZ6@RTSX'


def bar(day):
    return Candle(datetime(2026,11,day,8,tzinfo=MOSCOW),10,12,9,11,100)


class FirstOpenFeed(FinamAPI):
    # Regression fixture for the retired, still-readable MOEX family.
    # Production FinamAPI defaults to native D/W/M and H4 from H1.
    h1_only_h4_native_dwm=False
    def __init__(self):
        super().__init__(secret_reader=lambda:'')
        self.calls=[];self.hook=None
        self.rows={'D1':[bar(n) for n in (13,14,15,16)],
                   'W1':[bar(9),bar(16)],
                   'MN1':[Candle(datetime(2025,6,1,8,tzinfo=MOSCOW),20,22,19,21,200),bar(1)]}
        self.rows['H1']=[Candle(datetime(2026,11,n,10 if n in (14,15) else 7,tzinfo=MOSCOW),
                               10,12,9,11,100) for n in (13,14,15,16)]

    def market_delay_minutes(self,mic):return 0

    @staticmethod
    def archive_request(tf,start,end):return tf=='D1' and end-start<=timedelta(days=180)

    def bars(self,symbol,tf,*,start,end):
        self.calls.append((symbol,tf,start,end))
        if self.hook is not None:self.hook(tf,start,end)
        return [b for b in self.rows.get(tf,()) if start<=b.time<end]


class TestMoexFirstOpen(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'cache.sqlite3'
        self.log=Path(self.directory.name)/'diagnostics.log'
        log=patch('flowdesk.diagnostics_log.LOG_PATH',self.log)
        log.start();self.addCleanup(log.stop)
        self.api=FirstOpenFeed()
        self.market=FinamMarketService(self.path,api=self.api,clock=lambda:NOW)
        for tf,rows in self.api.rows.items():self.market._upsert(SYMBOL,tf,rows)

    def archive_calls(self):
        return [(a,b) for _,tf,a,b in self.api.calls if self.api.archive_request(tf,a,b)]

    def test_each_higher_frame_opens_before_any_scanner_pass_and_survives_restart(self):
        for tf in NATIVE_HIGHER:
            with self.subTest(tf=tf),tempfile.TemporaryDirectory(dir=self.directory.name) as folder:
                api=FirstOpenFeed()
                market=FinamMarketService(Path(folder)/'cache.sqlite',api=api,clock=lambda:NOW)
                for source,rows in api.rows.items():market._upsert(SYMBOL,source,rows)
                self.assertFalse(moex.archive_ready(market._series(SYMBOL),SYMBOL))
                main,_,_=market.view('CRZ6','RTSX',tf,allow_http=True)
                self.assertTrue(main.candles)
                self.assertEqual(main.session_info['version'],moex.POLICY)
                self.assertTrue(moex.archive_ready(market._series(SYMBOL),SYMBOL))
                self.assertLessEqual(sum(api.archive_request(frame,a,b) for _,frame,a,b in api.calls),4)
                before=len(api.calls)
                restarted=FinamMarketService(market.path,api=api,clock=lambda:NOW)
                cached,_,_=restarted.view('CRZ6','RTSX',tf,allow_http=False)
                self.assertEqual(cached.candles,main.candles)
                self.assertEqual(len(api.calls),before)
                self.assertEqual(market.prepare_series(SYMBOL,market._series(SYMBOL),NOW)['D1'][-1].trading_date,date(2026,11,16))

    def test_screener_bootstraps_moex_dwm_without_opening_chart(self):
        from flowdesk.finam_screener import scan_one
        self.api.latest_quote=lambda symbol:{"quote":[{"last":{"value":"11"}}]}
        self.assertFalse(moex.archive_ready(self.market._series(SYMBOL),SYMBOL))
        result=scan_one(self.market,"CRZ6","RTSX",IndicatorSettings())
        raw=self.market._series(SYMBOL)
        self.assertTrue(moex.archive_ready(raw,SYMBOL))
        prepared=self.market.prepare_series(SYMBOL,raw,NOW)
        self.assertTrue(all(prepared.get(tf) for tf in ('D1','W1','MN1')))
        self.assertTrue(all("нет свечей" not in result.reason_map()[tf]
                            for tf in ('D','W','M')))
        # Ready family must not enter the MOEX archive bootstrap again.
        # A normal overlapping D1 HTTP refresh is allowed on each scan.
        with patch("flowdesk.moex_trading_family.backfill",wraps=moex.backfill) as bootstrap:
            scan_one(self.market,"CRZ6","RTSX",IndicatorSettings())
            bootstrap.assert_not_called()

    def test_screener_rechecks_stale_closed_native_d1_without_archive_reset(self):
        from contextlib import closing
        from flowdesk.finam_screener import scan_one
        old=self.api.rows['D1'][0].time
        self.market.save_history_progress(SYMBOL,moex.PROGRESS,{
            'cursor':datetime(2025,2,24,tzinfo=MOSCOW),'exhausted':True})
        with closing(self.market._connection()) as db,db:
            db.execute("UPDATE candle_source_observations SET observed_at=? "
                       "WHERE symbol=? AND tf='D1' AND ts=?",
                       (old.isoformat(),SYMBOL,old.isoformat()))
        raw=self.market._series(SYMBOL)
        self.assertTrue(moex.archive_ready(raw,SYMBOL))
        self.assertEqual(moex._unconfirmed_snapshot(raw,SYMBOL,NOW),('D1',old))
        first=self.market.prepare_series(SYMBOL,raw,NOW)
        self.assertEqual(first.session_metadata['issues']['D1'],
                         'native_snapshot_unconfirmed')
        self.api.latest_quote=lambda symbol:{'quote':[{'last':{'value':'11'}}]}
        # The normal three-bar tail has already moved beyond the old draft.
        with patch.object(self.market,'refresh_series',return_value={}):
            result=scan_one(self.market,'CRZ6','RTSX',IndicatorSettings())
        self.assertTrue(all('нет свечей' not in result.reason_map()[tf]
                            for tf in ('D','W','M')))
        raw=self.market._series(SYMBOL)
        self.assertIsNone(moex._unconfirmed_snapshot(raw,SYMBOL,NOW))
        prepared=self.market.prepare_series(SYMBOL,raw,NOW)
        self.assertTrue(all(prepared.get(tf) for tf in ('D1','W1','MN1')))
        requests=[(frame,a,b) for _,frame,a,b in self.api.calls
                  if frame=='D1' and a<=old<b]
        self.assertEqual(len(requests),1)
        self.assertLessEqual(requests[0][2]-requests[0][1],
                             timedelta(days=30,hours=1))
        with patch.object(self.market,'refresh_series',return_value={}):
            scan_one(self.market,'CRZ6','RTSX',IndicatorSettings())
        self.assertEqual(len([(frame,a,b) for _,frame,a,b in self.api.calls
                              if frame=='D1' and a<=old<b]),1)

    def test_empty_recheck_never_confirms_unreturned_native_d1(self):
        from contextlib import closing
        from flowdesk.finam_screener import scan_one
        old=self.api.rows['D1'][0].time
        self.market.save_history_progress(SYMBOL,moex.PROGRESS,{
            'cursor':datetime(2025,2,24,tzinfo=MOSCOW),'exhausted':True})
        with closing(self.market._connection()) as db,db:
            db.execute("UPDATE candle_source_observations SET observed_at=? "
                       "WHERE symbol=? AND tf='D1' AND ts=?",
                       (old.isoformat(),SYMBOL,old.isoformat()))
        self.api.latest_quote=lambda symbol:{'quote':[{'last':{'value':'11'}}]}
        original=self.api.bars
        self.api.bars=lambda symbol,tf,*,start,end:(
            [] if tf=='D1' and start<=old<end else
            original(symbol,tf,start=start,end=end))
        with patch.object(self.market,'refresh_series',return_value={}):
            result=scan_one(self.market,'CRZ6','RTSX',IndicatorSettings())
        self.assertIsNone(result.as_map()['D'])
        self.assertIn('Finam ещё не подтвердил исходную дневную свечу',
                      result.reason_map()['D'])
        self.assertEqual(self.market.prepare_series(SYMBOL,
                         self.market._series(SYMBOL),NOW).session_metadata[
                         'issues']['D1'],'native_snapshot_unconfirmed')

    def test_screener_surfaces_finam_bootstrap_denial_without_signals(self):
        from flowdesk.finam_screener import scan_one
        self.api.latest_quote=lambda symbol:{"quote":[{"last":{"value":"11"}}]}
        def denied(tf,start,end):
            if self.api.archive_request(tf,start,end):
                raise FinamAPIError('SECRET',http_status=403)
        self.api.hook=denied
        result=scan_one(self.market,"CRZ6","RTSX",IndicatorSettings())
        for tf in ('D','W','M'):
            self.assertIsNone(result.as_map()[tf])
            self.assertIn("нет доступа к истории Finam (403)",result.reason_map()[tf])
            self.assertNotIn("SECRET",result.reason_map()[tf])

    def test_loader_delivers_chart_after_bootstrap_instead_of_failure(self):
        from flowdesk.finam_gui import FinamLoadWorker
        loaded=[];failed=[]
        loader=FinamLoadWorker(self.market,'CRZ6','RTSX','D1',IndicatorSettings(),allow_http=True)
        loader.loaded.connect(lambda main,parents,info:loaded.append(main))
        loader.failed.connect(failed.append)
        loader.run()
        self.assertFalse(failed)
        self.assertEqual(loaded[0].candles[-1].trading_date,date(2026,11,16))

    def test_cached_only_access_never_requests_network_and_names_pending_history(self):
        with self.assertRaisesRegex(RuntimeError,'подготовка дневной истории Финама ещё не завершена'):
            self.market.view('CRZ6','RTSX','D1',allow_http=False)
        self.assertEqual(self.api.calls,[])

    def test_cancelled_bootstrap_persists_completed_window_and_resumes_from_it(self):
        stop=threading.Event();count=[0]
        def hook(tf,start,end):
            if self.api.archive_request(tf,start,end):
                count[0]+=1
                if count[0]==2:stop.set()
        self.api.hook=hook
        with self.assertRaises(InterruptedError):
            self.market.view('CRZ6','RTSX','D1',allow_http=True,cancelled=stop.is_set)
        progress=self.market.load_history_progress()[(SYMBOL,moex.PROGRESS)]
        self.assertFalse(progress['exhausted'])
        self.assertEqual(progress['cursor'],NOW-timedelta(days=180))
        self.assertFalse(moex.archive_ready(self.market._series(SYMBOL),SYMBOL))
        stop.clear();self.api.hook=None;before=len(self.archive_calls())
        main,_,_=self.market.view('CRZ6','RTSX','D1',allow_http=True)
        self.assertTrue(main.candles)
        self.assertEqual(self.archive_calls()[before][1],progress['cursor'])

    def test_chart_and_background_share_one_bootstrap_without_duplicate_windows(self):
        entered=threading.Event();release=threading.Event();foreground=threading.Event()
        def hook(tf,start,end):
            if self.api.archive_request(tf,start,end) and not entered.is_set():
                entered.set()
                if not release.wait(5):raise TimeoutError()
        self.api.hook=hook
        with ThreadPoolExecutor(max_workers=2) as pool:
            background=pool.submit(moex.backfill,self.market,SYMBOL,NOW,cancelled=lambda:False)
            try:
                self.assertTrue(entered.wait(2))
                chart=pool.submit(self.market.view,'CRZ6','RTSX','D1',allow_http=True,
                    progress=lambda text:foreground.set() if 'подготовка' in text else None)
                self.assertTrue(foreground.wait(2))
            finally:release.set()
            background.result(timeout=10)
            self.assertTrue(chart.result(timeout=10)[0].candles)
        windows=self.archive_calls()
        self.assertEqual(len(windows),3)
        self.assertEqual(len(set(windows)),3)

    def test_bootstrap_failure_has_safe_diagnostics_and_no_raw_error_in_loader(self):
        def hook(tf,start,end):
            if self.api.archive_request(tf,start,end):raise FinamAPIError('SENTINEL_SECRET',http_status=503)
        self.api.hook=hook
        with self.assertRaises(FinamAPIError) as caught:
            self.market.view('CRZ6','RTSX','D1',allow_http=True)
        self.assertEqual(caught.exception.http_status,503)
        self.assertNotIn('SENTINEL_SECRET',str(caught.exception))
        self.assertFalse(moex.archive_ready(self.market._series(SYMBOL),SYMBOL))
        logged=self.log.read_text(encoding='utf-8')
        self.assertIn('timeframe=D1',logged)
        self.assertIn('http=http_503',logged)
        self.assertNotIn('SENTINEL_SECRET',logged)

    def add_older_opening(self):
        native=Candle(datetime(2026,10,17,7,tzinfo=MOSCOW),10,15,5,11,100)
        source=Candle(datetime(2026,10,17,10,tzinfo=MOSCOW),12,15,5,11,100)
        self.api.rows['D1'].insert(0,native)
        self.api.rows['H1'].insert(0,source)
        self.market._upsert(SYMBOL,'D1',[native])
        return source

    def test_old_opening_outside_normal_refresh_is_loaded_on_first_chart_open(self):
        source=self.add_older_opening()
        main,_,_=self.market.view('CRZ6','RTSX','W1',allow_http=True)
        selected=next(b for b in main.candles if b.trading_date==date(2026,10,19))
        self.assertEqual(selected.open,12)
        requests=[(a,b) for _,tf,a,b in self.api.calls if tf=='H1']
        self.assertTrue(any(a<=source.time<b for a,b in requests))
        self.assertTrue(all(b-a<=timedelta(days=30) for a,b in requests))
        before=len(self.api.calls)
        restarted=FinamMarketService(self.path,api=self.api,clock=lambda:NOW)
        cached,_,_=restarted.view('CRZ6','RTSX','W1',allow_http=False)
        self.assertEqual(cached.candles,main.candles)
        self.assertEqual(len(self.api.calls),before)

    def test_opening_bootstrap_cancellation_resumes_completed_h1_cursor(self):
        self.add_older_opening();stop=threading.Event();count=[0]
        def hook(tf,start,end):
            if tf=='H1':
                count[0]+=1
                if count[0]==3:stop.set()
        self.api.hook=hook
        with self.assertRaises(InterruptedError):
            self.market.view('CRZ6','RTSX','W1',allow_http=True,cancelled=stop.is_set)
        progress=self.market.load_history_progress()[(SYMBOL,moex.OPEN_PROGRESS)]
        self.assertEqual(progress['cursor'],NOW-timedelta(days=30))
        stop.clear();self.api.hook=None;before=len(self.api.calls)
        main,_,_=self.market.view('CRZ6','RTSX','W1',allow_http=True)
        self.assertTrue(main.candles)
        self.assertTrue(any(tf=='H1' and end==progress['cursor'] for _,tf,_,end in self.api.calls[before:]))
