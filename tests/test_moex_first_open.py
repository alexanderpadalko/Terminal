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
