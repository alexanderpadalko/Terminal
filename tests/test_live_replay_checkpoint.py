"""Changing live suffixes must exactly match complete pinned replay."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import gc
import unittest
from unittest.mock import patch
import weakref

from flowdesk.engine import analyze
from flowdesk.history_analysis import analyze_history
from flowdesk.market_data import Candle
from flowdesk.reference_engine import PinnedFlowEngine
from flowdesk.reference_runtime import ReferenceContext,program
from flowdesk.settings import IndicatorSettings
from test_resource_optimization import numeric


def bars(tf='H1',count=100):
    seconds={'M1':60,'H1':3600,'H4':14400}[tf]
    start=datetime(2026,10,1,tzinfo=timezone.utc)
    result=[]
    for i in range(count):
        price=100+(i%13-6)*.6+(i%7)*.13
        result.append(Candle(start+timedelta(seconds=i*seconds),price,price+1+(i%3)*.4,
                             price-1-(i%5)*.2,price+(i%2-.5)*.3,10+i))
    return result


class LiveReplayTests(unittest.TestCase):
    def setUp(self):
        self.rows=bars();self.cfg=IndicatorSettings()
        self.asof=self.rows[-1].time+timedelta(minutes=20)

    def test_changing_live_tail_replays_one_step_and_preserves_previous_snapshots(self):
        old=analyze(self.rows,'H1',asof=self.asof,settings=self.cfg)
        protected=deepcopy((old.segments,old.fvg,old.flows,old.pairs,old.market))
        changed=[*self.rows[:-1],replace(self.rows[-1],high=self.rows[-1].high+3)]
        step=PinnedFlowEngine.step
        with patch.object(PinnedFlowEngine,'step',autospec=True,side_effect=step) as replay:
            got=analyze(changed,'H1',asof=self.asof+timedelta(seconds=1),settings=self.cfg,cached=old)
        self.assertEqual(replay.call_count,1)
        numeric(self,got,analyze(changed,'H1',asof=got.asof,settings=self.cfg))
        self.assertEqual((old.segments,old.fvg,old.flows,old.pairs,old.market),protected)
        self.assertIs(got.replay_checkpoint,old.replay_checkpoint)
        again=analyze(self.rows,'H1',asof=got.asof,settings=self.cfg,cached=got)
        numeric(self,again,analyze(self.rows,'H1',asof=got.asof,settings=self.cfg))

    def test_closing_boundary_and_historical_correction_force_full_replay(self):
        old=analyze(self.rows,'H1',asof=self.asof,settings=self.cfg)
        fixed=list(self.rows);fixed[4]=replace(fixed[4],low=fixed[4].low-4)
        for rows,asof in ((fixed,self.asof),(self.rows,self.asof+timedelta(hours=1))):
            step=PinnedFlowEngine.step
            with patch.object(PinnedFlowEngine,'step',autospec=True,side_effect=step) as replay:
                got=analyze(rows,'H1',asof=asof,settings=self.cfg,cached=old)
            self.assertEqual(replay.call_count,len(rows))
            numeric(self,got,analyze(rows,'H1',asof=asof,settings=self.cfg))

    def test_setting_tick_and_backwards_clock_changes_force_full_replay(self):
        old=analyze(self.rows,'H1',asof=self.asof,settings=self.cfg)
        changed=self.cfg.changed(fvgExcludeGaps=False)
        for cfg,tick,asof in ((changed,.01,self.asof),
                               (self.cfg,.001,self.asof),(self.cfg,.01,self.asof-timedelta(seconds=1))):
            step=PinnedFlowEngine.step
            with patch.object(PinnedFlowEngine,'step',autospec=True,side_effect=step) as replay:
                got=analyze(self.rows,'H1',asof=asof,settings=cfg,minimum_tick=tick,cached=old)
            self.assertEqual(replay.call_count,len(self.rows))
            numeric(self,got,analyze(self.rows,'H1',asof=asof,settings=cfg,minimum_tick=tick))

    def test_changed_closed_higher_source_invalidates_prefix_and_capture_audit(self):
        higher=analyze(bars('H4',25),'H4',asof=self.asof,settings=self.cfg)
        sources={'H4':higher}
        old=analyze(self.rows,'H1',asof=self.asof,settings=self.cfg,higher_liquidity=sources)
        fixed=list(higher.candles);fixed[2]=replace(fixed[2],high=fixed[2].high+5)
        updated={'H4':analyze(fixed,'H4',asof=self.asof,settings=self.cfg)}
        step=PinnedFlowEngine.step
        with patch.object(PinnedFlowEngine,'step',autospec=True,side_effect=step) as replay:
            got=analyze(self.rows,'H1',asof=self.asof,settings=self.cfg,higher_liquidity=updated,cached=old)
        self.assertEqual(replay.call_count,len(self.rows))
        numeric(self,got,analyze(self.rows,'H1',asof=self.asof,settings=self.cfg,higher_liquidity=updated))

    def test_capture_audit_is_identical_when_resuming_unconfirmed_suffix(self):
        sources={'H4':analyze(bars('H4',25),'H4',asof=self.asof,settings=self.cfg)}
        old=analyze(self.rows,'H1',asof=self.asof,settings=self.cfg,higher_liquidity=sources)
        rows=[*self.rows[:-1],replace(self.rows[-1],low=self.rows[-1].low-6)]
        with patch('flowdesk.liquidity.audit_higher_captures',side_effect=AssertionError('Unchanged closed audit replayed')):
            got=analyze(rows,'H1',asof=self.asof,settings=self.cfg,higher_liquidity=sources,cached=old)
        numeric(self,got,analyze(rows,'H1',asof=self.asof,settings=self.cfg,higher_liquidity=sources))

    def test_low_timeframe_h1_projection_remains_fresh(self):
        lower=bars('M1',100);asof=lower[-1].time+timedelta(seconds=20)
        h1=analyze(bars('H1',3),'H1',asof=asof,settings=self.cfg)
        old=analyze(lower,'M1',asof=asof,settings=self.cfg,higher_h1=h1)
        changed=[*lower[:-1],replace(lower[-1],high=lower[-1].high+2)]
        got=analyze(changed,'M1',asof=asof,settings=self.cfg,higher_h1=h1,cached=old)
        numeric(self,got,analyze(changed,'M1',asof=asof,settings=self.cfg,higher_h1=h1))

    def test_history_block_offsets_preserve_checkpoint_local_indices(self):
        rows=self.rows[:25]+[replace(b,time=b.time+timedelta(days=2)) for b in self.rows[25:]]
        breaks=[rows[25].time.isoformat()];asof=rows[-1].time+timedelta(minutes=20)
        old=analyze_history(rows,'H1',breaks=breaks,asof=asof,settings=self.cfg)
        for i in range(2):
            rows=[*rows[:-1],replace(rows[-1],high=rows[-1].high+1)]
            got=analyze_history(rows,'H1',breaks=breaks,asof=asof,settings=self.cfg,cached=old)
            numeric(self,got,analyze_history(rows,'H1',breaks=breaks,asof=asof,settings=self.cfg))
            old=got

    def test_all_closed_history_retains_no_checkpoint(self):
        result=analyze(self.rows,'H1',asof=self.asof+timedelta(hours=1),settings=self.cfg)
        self.assertIsNone(result.replay_checkpoint)

    def test_all_seven_timeframes_match_full_replay_on_repeated_live_changes(self):
        durations={'M1':60,'M5':300,'H1':3600,'H4':14400,'D1':86400,'W1':604800,'MN1':2678400}
        start=datetime(2021,1,1,tzinfo=timezone.utc)
        for tf,seconds in durations.items():
            with self.subTest(tf=tf):
                rows=[replace(b,time=start+timedelta(seconds=i*seconds),
                              scheduled_close=start+timedelta(seconds=(i+1)*seconds))
                      for i,b in enumerate(bars(count=60))]
                observed=rows[-1].time+timedelta(seconds=seconds//2)
                previous=analyze(rows,tf,asof=observed,settings=self.cfg)
                for _ in range(3):
                    rows=[*rows[:-1],replace(rows[-1],high=rows[-1].high+.25,volume=rows[-1].volume+1)]
                    got=analyze(rows,tf,asof=observed,settings=self.cfg,cached=previous)
                    numeric(self,got,analyze(rows,tf,asof=observed,settings=self.cfg))
                    previous=got

    def test_reference_context_is_released_without_cyclic_gc(self):
        was_enabled=gc.isenabled();gc.disable()
        try:
            context=ReferenceContext('H1',self.cfg)
            data,functions,sections=program()
            custom={**sections,'test':compile('value=3','<test>','exec')}
            with patch('flowdesk.reference_runtime.program',return_value=(data,functions,custom)):
                context.section('test')
            ref=weakref.ref(context)
            del context
            self.assertIsNone(ref())
        finally:
            if was_enabled:gc.enable()


if __name__=='__main__':unittest.main()
