"""Resource budgets and exact replay parity without shortening any history."""
from contextlib import closing
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile,threading,unittest

from flowdesk.engine import analyze
from flowdesk.history_analysis import analyze_history
from flowdesk.history_archive import HistoryArchiveWorker
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle
from flowdesk.pipeline import analyze_timeframes
from flowdesk.session_candles import _LowerSource
from flowdesk.session_store import record_coverage
from flowdesk.settings import IndicatorSettings
from flowdesk.trading_calendar import calendar_for
from test_history_retention import hours
from test_finam_optimization import Feed,NOW
from test_session_candles import lower


def numeric(test,a,b):
    for name in ('candles','segments','fvg','flows','pairs','confirmed_fvg','market',
                 'liquidity_audit','asof','minimum_tick','flow_source_tf','flow_status'):
        test.assertEqual(getattr(a,name),getattr(b,name),name)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=FinamMarketService(Path(self.tmp.name)/'data.sqlite',api=Feed(),clock=lambda:NOW)
        self.settings=IndicatorSettings()

    def test_tail_update_reuses_unchanged_blocks_and_matches_full_replay(self):
        start=datetime(2026,10,1,tzinfo=timezone.utc)
        rows=hours(start,25)+hours(start+timedelta(days=3),30)
        breaks=[rows[25].time.isoformat()];asof=rows[-1].time+timedelta(minutes=20)
        first=analyze_history(rows,'H1',breaks=breaks,asof=asof)
        updated=[*rows[:-1],replace(rows[-1],high=rows[-1].high+2)]
        with patch('flowdesk.history_analysis.analyze',wraps=analyze) as replay:
            cached=analyze_history(updated,'H1',breaks=breaks,asof=asof,cached=first)
        self.assertEqual(replay.call_count,1)
        numeric(self,cached,analyze_history(updated,'H1',breaks=breaks,asof=asof))

    def test_prepending_older_block_rebases_reused_indices_exactly(self):
        start=datetime(2026,10,1,tzinfo=timezone.utc)
        rows=hours(start,25)+hours(start+timedelta(days=3),30)
        asof=rows[-1].time+timedelta(hours=1);breaks=[rows[25].time.isoformat()]
        old=analyze_history(rows,'H1',breaks=breaks,asof=asof)
        prefix=hours(start-timedelta(days=3),20)
        expanded=prefix+rows;boundaries=[rows[0].time.isoformat(),*breaks]
        with patch('flowdesk.history_analysis.analyze',wraps=analyze) as replay:
            cached=analyze_history(expanded,'H1',breaks=boundaries,asof=asof,cached=old)
        self.assertEqual(replay.call_count,1)
        numeric(self,cached,analyze_history(expanded,'H1',breaks=boundaries,asof=asof))

    def test_interior_correction_and_closing_boundary_do_not_reuse_stale_block(self):
        start=datetime(2026,10,1,tzinfo=timezone.utc)
        rows=hours(start,25)+hours(start+timedelta(days=3),30)
        breaks=[rows[25].time.isoformat()];asof=rows[-1].time+timedelta(minutes=20)
        old=analyze_history(rows,'H1',breaks=breaks,asof=asof)
        fixed=list(rows);fixed[2]=replace(fixed[2],low=fixed[2].low-3)
        later=asof+timedelta(hours=1)
        got=analyze_history(fixed,'H1',breaks=breaks,asof=later,cached=old)
        numeric(self,got,analyze_history(fixed,'H1',breaks=breaks,asof=later))

    def test_warm_pipeline_uses_unchanged_timeframes_and_keeps_exact_result(self):
        start=NOW-timedelta(days=20)
        series={'H1':hours(start,60),'H4':hours(start,20),'D1':hours(start,15)}
        first=self.service.analyze_cached('TEST@RTSX','H1',series,self.settings,NOW,terminal=False)
        changed={**series,'H1':[*series['H1'][:-1],replace(series['H1'][-1],high=120)]}
        cached=self.service.analyze_cached('TEST@RTSX','H1',changed,self.settings,NOW,terminal=False)
        fresh=analyze_timeframes(changed,'H1',settings=self.settings,asof=NOW)
        numeric(self,cached[0],fresh[0])
        for tf in cached[1]:numeric(self,cached[1][tf],fresh[1][tf])
        self.assertIs(cached[1]['D1'],first[1]['D1'])
        self.assertNotIn(cached[0],cached[0].summary_sources.values())

    def test_unused_minute_archive_is_not_retained_by_hourly_fingerprint(self):
        series={'H1':self.service.api.rows,'M1':hours(NOW-timedelta(days=10),2000)}
        first=self.service.analyze_cached('TEST@RTSX','H1',series,self.settings,NOW,terminal=False)
        changed={**series,'M1':[*series['M1'],Candle(NOW,100,101,99,100,1)]}
        again=self.service.analyze_cached('TEST@RTSX','H1',changed,self.settings,NOW,terminal=False)
        self.assertIs(first,again)
        key=self.service.analysis_cache['TEST@RTSX','H1'][0]
        self.assertEqual({entry[0] for entry in key[1]},{'H1'})

    def test_lru_budget_keeps_active_chart_without_deleting_disk_history(self):
        rows=self.service.api.rows;symbol='KEEP@RTSX'
        self.service._upsert(symbol,'H1',rows)
        self.service._active_analysis_key=(symbol,'H1')
        with patch('flowdesk.finam_market.ANALYSIS_CACHE_ENTRIES',2):
            for name in (symbol,'A@RTSX','B@RTSX','C@RTSX'):
                self.service.analyze_cached(name,'H1',{'H1':rows},self.settings,NOW,terminal=False)
        self.assertEqual(len(self.service.analysis_cache),2)
        self.assertIn((symbol,'H1'),self.service.analysis_cache)
        self.assertEqual(self.service._series(symbol)['H1'],rows)

    def test_chart_reuses_unaffected_buckets_and_matches_full_after_old_correction(self):
        symbol='SBER@MISX'
        calendar=calendar_for(symbol)
        sessions=[calendar.session(date(2026,11,day)) for day in (17,18,19)]
        observed=sessions[-1].end+timedelta(minutes=2)
        api=SimpleNamespace(session_candles=True,market_delay_minutes=lambda _:0)
        market=FinamMarketService(Path(self.tmp.name)/'stock.sqlite',api=api,clock=lambda:observed)
        sources=[bar for session in sessions for bar in lower(session)]
        market._upsert(symbol,'M5',sources)
        record_coverage(market,symbol,'M5',sessions[0].start,sessions[-1].end)
        bucket=_LowerSource._bucket
        with patch.object(_LowerSource,'_bucket',autospec=True,side_effect=bucket) as calls:
            first,_,_=market.view('SBER','MISX','D1')
            cold_calls=calls.call_count
            calls.reset_mock()
            # The correction affects the first trading day of the loaded range.
            corrected=replace(sources[0],high=1000)
            market._upsert(symbol,'M5',[corrected])
            updated,parents,_=market.view('SBER','MISX','D1')
            self.assertGreater(calls.call_count,0)
            self.assertLess(calls.call_count,cold_calls)
        self.assertNotEqual(updated.candles,first.candles)

        with closing(market._connection()) as db,db:
            db.execute('DELETE FROM session_series_cache WHERE symbol=?',(symbol,))
        fresh_market=FinamMarketService(market.path,api=api,clock=lambda:observed)
        fresh,fresh_parents,_=fresh_market.view('SBER','MISX','D1')
        numeric(self,updated,fresh)
        self.assertEqual(updated.session_info,fresh.session_info)
        self.assertEqual(set(parents),set(fresh_parents))
        for tf in parents:
            numeric(self,parents[tf],fresh_parents[tf])
        numeric(self,updated.summary_sources['H1'],fresh.summary_sources['H1'])

    def test_archive_pacing_is_interruptible_and_defaults_to_one_worker(self):
        entry=SimpleNamespace(symbol='TEST@RTSX',verified=True)
        worker=HistoryArchiveWorker(self.service,[entry])
        self.assertEqual(worker.max_workers,1)
        entered=threading.Event()
        def window(*args,**kwargs):
            entered.set();return {'changed':False}
        with (patch('flowdesk.history_archive.backfill_window',side_effect=window),
              patch('flowdesk.history_archive.time.thread_time',side_effect=[0,5])):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                worker.requestInterruption()
                self.assertTrue(worker.wait(1500))
            finally:
                worker.requestInterruption();worker.wait(3000)


if __name__=='__main__':unittest.main()
