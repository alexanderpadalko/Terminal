"""Resource budgets and exact replay parity without shortening any history."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile,threading,unittest

from flowdesk.engine import analyze
from flowdesk.history_analysis import analyze_history
from flowdesk.history_archive import HistoryArchiveWorker
from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_screener import scan_one
from flowdesk.market_data import Candle
from flowdesk.pipeline import analyze_timeframes
from flowdesk.settings import IndicatorSettings
from test_history_retention import hours
from test_finam_optimization import Feed,NOW


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

    def test_scanner_reads_source_archive_once_per_instrument(self):
        with patch.object(self.service,'_series',wraps=self.service._series) as read:
            scan_one(self.service,'TEST','RTSX',self.settings)
        self.assertEqual(read.call_count,1)

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
