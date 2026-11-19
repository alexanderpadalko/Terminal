import tempfile
import threading
import unittest
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowdesk.finam_api import FinamAPIError, DEPTH_DAYS
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle
from flowdesk.history_archive import backfill_window, HistoryArchiveWorker, VERSION, FLOOR, FRAMES, LEGACY_FRAMES

UTC=timezone.utc
NOW=datetime(2026,11,16,12,tzinfo=UTC)


class ArchiveAPI:
    session_candles=False
    def __init__(self):
        self.calls=[]
        self.scopes=[]
        self.answer=lambda start,end: [Candle(start,10,11,9,10,1)]
    @contextmanager
    def request_scope(self,**kwargs):
        self.scopes.append(kwargs)
        yield
    def bars(self,symbol,tf,*,start,end):
        self.calls.append((symbol,tf,start,end))
        return self.answer(start,end)


class FullArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api=ArchiveAPI()
        self.path=Path(self.folder.name)/'finam.sqlite'
        self.service=FinamMarketService(self.path,api=self.api,clock=lambda:NOW)
        self.symbol='CRZ6@RTSX'

    def test_legacy_archive_frames_move_and_resume_after_restart(self):
        self.assertNotIn('H1',FRAMES)  # Production history never archives H1.
        for _ in LEGACY_FRAMES:
            backfill_window(self.service,self.symbol)
        self.assertEqual([c[1] for c in self.api.calls],list(LEGACY_FRAMES))
        self.assertTrue(all(c[0]==self.symbol and c[3]-c[2]==timedelta(days=DEPTH_DAYS[c[1]])
                            for c in self.api.calls))
        before=self.service.load_history_progress()
        reloaded=FinamMarketService(self.path,api=self.api,clock=lambda:NOW)
        backfill_window(reloaded,self.symbol)
        self.assertEqual(self.api.calls[-1][3],before[self.symbol,VERSION+'MN1']['cursor'])
        self.assertTrue(all(s['priority']==30 for s in self.api.scopes))

    def test_old_exhausted_limits_and_existing_holes_do_not_stop_new_walk(self):
        self.service._upsert(self.symbol,'H1',[Candle(NOW-timedelta(days=200),10,11,9,10,1)])
        self.service.save_history_progress(self.symbol,'SESSION_H1',{'cursor':NOW-timedelta(days=30),'exhausted':True})
        self.service.save_history_progress(self.symbol,'H1',{'cursor':NOW-timedelta(days=30),'exhausted':True})
        backfill_window(self.service,self.symbol,frame='H1')
        self.assertEqual(self.api.calls[-1][3],NOW)  # cover interior, not just older MIN(ts)
        self.assertEqual(len(self.service._series(self.symbol)['H1']),2)

    def test_mixed_case_finam_contract_uses_existing_normalized_storage_key(self):
        backfill_window(self.service,'SiZ6@RTSX',frame='H1')
        self.assertEqual(self.api.calls[-1][0],'SIZ6@RTSX')
        self.assertIn(('SIZ6@RTSX',VERSION+'H1'),self.service.load_history_progress())
        self.assertEqual(len(self.service._series('SIZ6@RTSX')['H1']),1)

    def test_revalidated_unchanged_prices_still_notify_chart_of_source_evidence(self):
        bar=Candle(NOW-timedelta(days=30),10,11,9,10,1)
        self.service._upsert(self.symbol,'H1',[bar])
        result=backfill_window(self.service,self.symbol,frame='H1')
        self.assertTrue(result['changed'])
        self.assertEqual(self.service._series(self.symbol)['H1'],[bar])

    def test_empty_windows_continue_to_older_real_data(self):
        self.api.answer=lambda start,end: [] if start>NOW-timedelta(days=50) else [Candle(start,10,11,9,10,1)]
        backfill_window(self.service,self.symbol,frame='H1')
        self.assertFalse(self.service.load_history_progress()[self.symbol,VERSION+'H1']['exhausted'])
        backfill_window(self.service,self.symbol,frame='H1')
        self.assertEqual(len(self.service._series(self.symbol)['H1']),1)
        self.assertEqual(self.api.calls[-1][3],NOW-timedelta(days=30))

    def test_completion_only_at_search_floor(self):
        self.service.save_history_progress(self.symbol,VERSION+'W1',{'cursor':FLOOR+timedelta(days=1),'exhausted':False})
        backfill_window(self.service,self.symbol,frame='W1')
        self.assertEqual(self.api.calls[-1][2],FLOOR)
        self.assertTrue(self.service.load_history_progress()[self.symbol,VERSION+'W1']['exhausted'])
        self.assertIsNone(backfill_window(self.service,self.symbol,frame='W1'))

    def test_source_proven_first_bar_bounds_binance_without_guessing_onboard_date(self):
        first=NOW-timedelta(days=10)
        self.api.archive_start=lambda symbol,tf:first
        backfill_window(self.service,self.symbol,frame='H1')
        self.assertEqual(self.api.calls[-1][2],first)
        self.assertTrue(self.service.load_history_progress()[self.symbol,VERSION+'H1']['exhausted'])
        self.assertIsNone(backfill_window(self.service,self.symbol,frame='H1'))

    def test_errors_and_cancellation_never_advance_cursor(self):
        def failed(start,end):raise FinamAPIError('private',http_status=429)
        self.api.answer=failed
        with self.assertRaises(FinamAPIError):backfill_window(self.service,self.symbol)
        self.assertEqual(self.service.load_history_progress(),{})
        cancelled=threading.Event()
        def interrupted(start,end):
            cancelled.set()
            return [Candle(start,10,11,9,10,1)]
        self.api.answer=interrupted
        with self.assertRaises(InterruptedError):
            backfill_window(self.service,self.symbol,cancelled=cancelled.is_set)
        self.assertEqual(self.service.load_history_progress(),{})
        self.assertEqual(self.service._series(self.symbol),{})

    def test_network_holds_no_database_lock_and_token_change_cannot_publish(self):
        def answer(start,end):
            with closing(self.service._connection()) as db, db:
                db.execute('INSERT INTO full_archive_turns VALUES(?,?,0)',('OTHER@RTSX','M1'))
            self.service.invalidate_network_cache()
            return [Candle(start,10,11,9,10,1)]
        self.api.answer=answer
        with self.assertRaises(InterruptedError):backfill_window(self.service,self.symbol)
        self.assertEqual(self.service._series(self.symbol),{})
        self.assertEqual(self.service.load_history_progress(),{})

    def test_same_symbol_concurrent_windows_are_serial_and_idempotent(self):
        errors=[]
        def run():
            try:backfill_window(self.service,self.symbol,frame='H1')
            except Exception as exc:errors.append(type(exc).__name__)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertEqual(errors,[])
        self.assertEqual(len(self.api.calls),2)
        self.assertEqual(self.api.calls[1][3],self.api.calls[0][2])

    def test_binance_worker_is_included_and_uses_separate_service(self):
        crypto=FinamMarketService(Path(self.folder.name)/'binance.sqlite',api=ArchiveAPI(),clock=lambda:NOW)
        entries=[SimpleNamespace(symbol=s,verified=True) for s in (self.symbol,'BTCUSDT@BINANCE','ETHUSDT@BINANCE')]
        routing=SimpleNamespace(for_market=lambda mic:crypto if mic=='BINANCE' else self.service)
        calls=[]
        def bounded(service,symbol,**kwargs):
            calls.append((service.path.name,symbol))
            return None
        with patch('flowdesk.history_archive.backfill_window',side_effect=bounded):
            HistoryArchiveWorker(routing,entries,pace=False).run()
        self.assertEqual(set(calls),{('finam.sqlite',self.symbol),('binance.sqlite','BTCUSDT@BINANCE'),
                                     ('binance.sqlite','ETHUSDT@BINANCE')})

    def test_worker_rate_limit_stops_and_does_not_request_remaining_symbols(self):
        entries=[SimpleNamespace(symbol=f'CR{i}@RTSX',verified=True) for i in range(3)]
        def limited(*args,**kwargs):raise FinamAPIError('private',http_status=429)
        with patch('flowdesk.history_archive.backfill_window',side_effect=limited) as call:
            HistoryArchiveWorker(self.service,entries,max_workers=1,pace=False).run()
        self.assertEqual(call.call_count,1)

    def test_failed_timeframe_does_not_starve_other_frames_or_move_its_cursor(self):
        entries=[SimpleNamespace(symbol=self.symbol,verified=True)]
        calls=[]
        def bounded(service,symbol,**kwargs):
            if 'MN1' not in kwargs['excluded_frames']:
                exc=FinamAPIError('private',http_status=403)
                exc.archive_timeframe='MN1'
                raise exc
            calls.append(symbol)
            return None
        with patch('flowdesk.history_archive.backfill_window',side_effect=bounded):
            HistoryArchiveWorker(self.service,entries,max_workers=1,pace=False).run()
        self.assertEqual(calls,[self.symbol])
        self.assertEqual(self.service.load_history_progress(),{})

    def test_all_thirty_instruments_and_seven_frames_are_planned(self):
        entries=[SimpleNamespace(symbol=f'CR{i}@RTSX',verified=True) for i in range(28)]+[
            SimpleNamespace(symbol=s,verified=True) for s in ('BTCUSDT@BINANCE','ETHUSDT@BINANCE')]
        calls={}
        def bounded(service,symbol,**kwargs):
            calls[symbol]=calls.get(symbol,0)+1
            return {'changed':False} if calls[symbol]<=7 else None
        with patch('flowdesk.history_archive.backfill_window',side_effect=bounded):
            HistoryArchiveWorker(self.service,entries,pace=False).run()
        self.assertEqual(len(calls),30)
        self.assertEqual(set(calls.values()),{8})


if __name__=='__main__':unittest.main()
