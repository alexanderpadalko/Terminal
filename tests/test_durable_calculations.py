"""Restart/append/correction parity of disposable, non-executable snapshots."""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flowdesk.calculation_store import CalculationStore,encode,decode,model_version,SCHEMA
from flowdesk.engine import analyze
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import candle_close_time
from flowdesk.reference_engine import PinnedFlowEngine
from flowdesk.settings import IndicatorSettings
from flowdesk.pipeline import analyze_timeframes
from test_live_replay_checkpoint import bars
from test_finam_only import FakeAPI
from test_resource_optimization import numeric


class DurableCalculationsTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'source.sqlite'
        self.rows=bars('H4',100);self.cfg=IndicatorSettings()
        self.now=self.rows[-1].time+timedelta(minutes=20)

    def service(self):return FinamMarketService(self.path,api=FakeAPI(),clock=lambda:self.now)

    def test_restart_reuses_finished_calculation_and_populates_memory_cache(self):
        source=self.service()
        before=source.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
        self.assertTrue(source.calculations.path.is_file())
        restarted=self.service()
        with patch('flowdesk.finam_market.analyze_timeframes',side_effect=AssertionError('unnecessary replay')):
            after=restarted.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
            again=restarted.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
        numeric(self,before[0],after[0]);self.assertIs(after,again)
        self.assertEqual(len(restarted.analysis_cache),1)

    def test_restart_continues_across_new_closed_candle_and_matches_full_replay(self):
        before=self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)[0]
        changed=self.rows[:-1]+[replace(self.rows[-1],close=self.rows[-1].close+.2)]
        changed.append(replace(changed[-1],time=changed[-1].time+timedelta(hours=4)))
        later=self.now+timedelta(hours=4)
        step=PinnedFlowEngine.step
        with patch.object(PinnedFlowEngine,'step',autospec=True,side_effect=step) as calls:
            got=self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':changed},self.cfg,later)[0]
        self.assertLessEqual(calls.call_count,4)  # Full chart plus limited higher context.
        fresh=analyze_timeframes({'H4':changed},'H4',asof=later,settings=self.cfg,terminal=True)[0]
        numeric(self,got,fresh)
        self.assertGreater(len(got.replay_checkpoint.candles),len(before.replay_checkpoint.candles))

    def test_fully_closed_archive_has_a_resumable_checkpoint(self):
        observed=candle_close_time(self.rows[-1],'H4')+timedelta(minutes=1)
        before=analyze(self.rows,'H4',asof=observed,settings=self.cfg,market_structure=False,incremental=True)
        self.assertEqual(len(before.replay_checkpoint.candles),len(self.rows))
        newer=[*self.rows,replace(self.rows[-1],time=candle_close_time(self.rows[-1],'H4'))]
        restored=decode(encode(before))
        got=analyze(newer,'H4',asof=observed,settings=self.cfg,market_structure=False,incremental=True,cached=restored)
        fresh=analyze(newer,'H4',asof=observed,settings=self.cfg,market_structure=False)
        numeric(self,got,fresh)

    def test_corrected_closed_history_forces_complete_numeric_replay(self):
        self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
        changed=list(self.rows);changed[3]=replace(changed[3],low=changed[3].low-5)
        step=PinnedFlowEngine.step
        with patch.object(PinnedFlowEngine,'step',autospec=True,side_effect=step) as calls:
            got=self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':changed},self.cfg,self.now)[0]
        self.assertGreaterEqual(calls.call_count,len(changed)-1)
        numeric(self,got,analyze_timeframes({'H4':changed},'H4',asof=self.now,settings=self.cfg,terminal=True)[0])

    def test_changed_settings_or_backwards_clock_never_reuses_old_result(self):
        self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
        for cfg,observed in ((self.cfg.changed(fvgExcludeGaps=False),self.now),
                             (self.cfg,self.now-timedelta(hours=5))):
            got=self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},cfg,observed)[0]
            numeric(self,got,analyze_timeframes({'H4':self.rows},'H4',asof=observed,settings=cfg,terminal=True)[0])

    def test_bad_snapshot_or_changed_program_is_a_cache_miss(self):
        service=self.service()
        service.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
        with closing(sqlite3.connect(service.calculations.path)) as db,db:
            db.execute("UPDATE snapshots SET model='other program'")
        self.assertIsNone(service.calculations.load('EDZ6@RTSX','H4'))
        with closing(sqlite3.connect(service.calculations.path)) as db,db:
            db.execute("UPDATE snapshots SET payload=X'1234',model=?",(model_version(),))
        self.assertIsNone(service.calculations.load('EDZ6@RTSX','H4'))
        service.calculations.path.write_bytes(b'broken derived cache')
        self.assertIsNone(CalculationStore(service.calculations.path).load('EDZ6@RTSX','H4'))

    def test_unknown_serialized_classes_are_rejected(self):
        import json
        with self.assertRaises(ValueError):
            decode(json.dumps({'schema':SCHEMA,'root':{'ref':0},'nodes':[['eval',{}]]}))

    def test_frozen_model_revision_does_not_require_python_source_files(self):
        import json
        import flowdesk.calculation_store as module
        manifest=Path(module.__file__).parent/'data/calculation_model.json'
        expected=json.loads(manifest.read_text())['sha256']
        model_version.cache_clear()
        try:
            with patch('flowdesk.calculation_store.sys.frozen',True,create=True):
                self.assertEqual(model_version(),expected)
        finally:model_version.cache_clear()

    def test_derived_trading_dates_roundtrip_without_losing_session_metadata(self):
        row=replace(self.rows[-1],trading_date=self.rows[-1].time.date())
        self.assertEqual(decode(encode(row)),row)

    def test_cache_preserves_exchange_timezone_rules_and_dst_fold(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        spring=datetime(2026,3,1,tzinfo=ZoneInfo('America/Chicago'))
        restored=decode(encode(spring))
        self.assertEqual(restored.tzinfo.key,spring.tzinfo.key)
        self.assertEqual(restored.replace(month=4),spring.replace(month=4))
        autumn=datetime(2026,11,1,1,30,tzinfo=ZoneInfo('America/Chicago'),fold=1)
        saved=decode(encode(autumn))
        self.assertEqual((saved.timestamp(),saved.fold),(autumn.timestamp(),1))

    def test_snapshot_helpers_release_temporary_graphs_without_cyclic_gc(self):
        import gc,weakref
        enabled=gc.isenabled();gc.disable()
        try:
            result=analyze(self.rows,'H4',asof=self.now,market_structure=False,incremental=True)
            context=weakref.ref(result.replay_checkpoint.engines[-1].context)
            payload=encode(result)
            del result
            self.assertIsNone(context())
            restored=decode(payload)
            decoded_context=weakref.ref(restored.replay_checkpoint.engines[-1].context)
            del restored
            self.assertIsNone(decoded_context())
        finally:
            if enabled:gc.enable()

    def test_verified_tick_is_used_persisted_and_invalidates_older_precision(self):
        service=self.service()
        service.api.cached_tick_size=lambda symbol:.0001
        first=service.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)[0]
        self.assertEqual(first.minimum_tick,.0001)
        self.assertEqual(self.service().analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)[0].minimum_tick,.0001)
        service.api.cached_tick_size=lambda symbol:.001
        changed=service.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)[0]
        self.assertIsNot(first,changed)
        self.assertEqual(changed.minimum_tick,.001)

    def test_no_closed_candle_never_creates_a_confirmed_checkpoint(self):
        row=self.rows[-1]
        result=analyze([row],'H4',asof=row.time,settings=self.cfg,market_structure=False,incremental=True)
        self.assertIsNone(result.replay_checkpoint)

    def test_unconfirmed_ticks_do_not_rewrite_disk_snapshot(self):
        service=self.service()
        service.analyze_cached('EDZ6@RTSX','H4',{'H4':self.rows},self.cfg,self.now)
        with closing(sqlite3.connect(service.calculations.path)) as db:
            prior=db.execute('SELECT digest,touched FROM snapshots').fetchone()
        changed=self.rows[:-1]+[replace(self.rows[-1],high=self.rows[-1].high+1)]
        service.analyze_cached('EDZ6@RTSX','H4',{'H4':changed},self.cfg,self.now+timedelta(seconds=1))
        with closing(sqlite3.connect(service.calculations.path)) as db:
            self.assertEqual(db.execute('SELECT digest,touched FROM snapshots').fetchone(),prior)

    def test_multiple_timeframes_resume_and_reject_corrected_higher_history(self):
        from flowdesk.market_data import Candle
        from datetime import datetime,timezone
        end=datetime(2026,11,17,12,tzinfo=timezone.utc)
        def source(minutes,count=30):
            step=timedelta(minutes=minutes)
            return [Candle(end-step*(count-i),100+i%7,103+i%7,98+i%7,101+i%7,10,
                           scheduled_close=end-step*(count-i-1)) for i in range(count)]
        series={tf:source(step) for tf,step in [('H4',240),('D1',1440),('W1',10080),('MN1',43200)]}
        first,parents=analyze_timeframes(series,'H4',asof=end,settings=self.cfg,terminal=True)
        previous=decode(encode({'H4':first,**parents}))
        newer={tf:[*rows,replace(rows[-1],time=end,scheduled_close=end+timedelta(minutes=step))]
               for (tf,rows),step in zip(series.items(),(240,1440,10080,43200))}
        later=end+timedelta(hours=5)
        for correction in (False,True):
            changed={tf:list(rows) for tf,rows in newer.items()}
            if correction:changed['D1'][2]=replace(changed['D1'][2],high=200)
            actual,actual_parents=analyze_timeframes(changed,'H4',asof=later,settings=self.cfg,
                                                    cached=previous,terminal=True)
            fresh,fresh_parents=analyze_timeframes(changed,'H4',asof=later,settings=self.cfg,terminal=True)
            for tf,item in {'H4':actual,**actual_parents}.items():
                numeric(self,item,{'H4':fresh,**fresh_parents}[tf])
