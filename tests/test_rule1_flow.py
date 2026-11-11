"""Rule 1: the user's D1 scenario, priority, closure and source hierarchy."""
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from flowdesk.engine import Analysis, Candidate, FVG, FlowEngine, FlowState, PairSnapshot, StructurePoint, analyze
from flowdesk.liquidity import Capture, audit_higher_captures
from flowdesk.market_data import Candle, MOSCOW
from flowdesk.pipeline import analyze_timeframes


def candles(rows):
    return [Candle(datetime.fromisoformat(c['time']),
                   *(c[k] for k in ('open', 'high', 'low', 'close', 'volume'))) for c in rows]


class TestScreenshotScenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'afks_d1_rule1_screenshot.json').read_text())
        cls.days, cls.months = candles(fixture['days']), candles(fixture['months'])
        cls.asof = datetime.fromisoformat(fixture['asof'])

    def monthly_snapshot(self):
        # Externally supplied state: the screenshot's monthly guard and pair.
        # This tests consuming the Pine state, NOT recovering old monthly data.
        guard = FVG(0, 0, 0, 12.941, 12.777, -1)
        return Analysis(self.months, [], [guard],
                        [FlowState(i, -1, 5.02, 48.729, guard=guard) for i in range(len(self.months))],
                        [PairSnapshot(48.729, 5.02, 0, 0, -1) for _ in self.months], 'MN1')

    def test_real_daily_candles_with_supplied_pine_monthly_context(self):
        result = analyze(self.days, 'D1', asof=self.asof,
                         higher_liquidity={'MN1': self.monthly_snapshot()})
        event = next(f for c, f in zip(self.days, result.flows) if c.time.day == 10 and c.time.month == 11)
        self.assertEqual((event.rule, event.side), (1, -1))
        self.assertEqual(event.previous[2:4], (7.989, 9.306))
        self.assertEqual(event.rule1_check['first_fvg'], {'bottom': 8.67, 'top': 9.042})
        self.assertEqual(event.rule1_check['opposite_fvg']['bottom'], 9.054)
        self.assertEqual(event.rule1_check['opposite_fvg']['top'], 9.15)
        self.assertEqual(event.range_start_at.day, 8)
        confirmation = event.rule1_check['higher_capture']
        self.assertEqual(confirmation['source_tf'], 'MN1')
        self.assertEqual(confirmation['captured_at'][:10], '2026-06-30')
        self.assertEqual(result.flows[-1].side, -1)
        self.assertEqual(result.flows[-1].rule, 0)  # unfinished next day

    def test_missing_monthly_context_does_not_fabricate_screenshot_capture(self):
        result = analyze(self.days, 'D1', asof=self.asof, higher_liquidity={})
        event = result.flows[-2]
        self.assertEqual(event.side, 1)
        self.assertFalse(event.rule1_check['eligible'])
        self.assertIsNone(event.rule1_check['higher_capture'])

    def test_unfinished_daily_bar_preserves_previous_flow(self):
        target = self.days[-2]
        result = analyze(self.days[:-1], 'D1', asof=target.time + timedelta(hours=12),
                         higher_liquidity={'MN1': self.monthly_snapshot()})
        self.assertEqual(result.flows[-1].side, 1)
        self.assertEqual(result.flows[-1].rule, 0)
        self.assertFalse(result.flows[-1].rule1_check['applied'])

    def test_future_source_data_cannot_change_published_daily_states(self):
        full = analyze(self.days, 'D1', asof=self.asof,
                       higher_liquidity={'MN1': self.monthly_snapshot()})
        cutoff = self.days[-5].time + timedelta(days=1)
        early = analyze(self.days[:-4], 'D1', asof=cutoff,
                        higher_liquidity={'MN1': self.monthly_snapshot()})
        self.assertEqual([(f.side, f.lo, f.hi, f.rule, f.rule1_check) for f in early.flows],
                         [(f.side, f.lo, f.hi, f.rule, f.rule1_check) for f in full.flows[:len(early.flows)]])


BASE = datetime(2026, 11, 1, tzinfo=MOSCOW)


class TestRulePriorityAndCandidates(unittest.TestCase):
    def setup_flow(self, close=9.0, timeframe='H1'):
        bars = [Candle(BASE + timedelta(hours=i), close, 10, 8.5, close) for i in range(5)]
        engine = FlowEngine(timeframe)
        engine.side, engine.start, engine.lo, engine.hi = 1, 0, 8, 12
        engine.lo_i, engine.hi_i = 0, 1
        engine.guard = FVG(0, 0, 0, 10.5, 9.6, 1, broken=2)
        engine.guard_broken, engine.guard_broken_at = True, 2
        engine.rule_one.guard_key = (0, 0, 1, 10.5, 9.6)
        zone = FVG(1, 2, 3, 10.2, 10.0, -1)
        engine.rule_one.candidates = [Candidate(zone, 1, 8.5, 11, 2, 1, 3)]
        point = StructurePoint(4, -1, 1, 11, 3, 8.5, 0, None, None)
        capture = Capture('D1', -1, bars[2].time, 'FVG', 12, True, 13, 12)
        return engine, bars, point, capture

    def test_rule2_overrides_rule1(self):
        e, bars, raw, capture = self.setup_flow()
        e.lo = 9.2
        f = e.step(bars[4], 4, raw, None, bars, PairSnapshot(), capture=capture)
        self.assertEqual(f.rule, 2)
        self.assertEqual(f.rule1_check['outcome'], 'OVERRIDDEN_BY_RULE_2')
        self.assertFalse(f.rule1_check['applied'])

    def test_rule1_overrides_rule3(self):
        e, bars, raw, capture = self.setup_flow()
        e.candidate_cont = Candidate(FVG(1, 2, 3, 9, 8.8, 1), 1, 8, 12, 1, 2, 3)
        e.extension_i = 3
        f = e.step(bars[4], 4, raw, None, bars, PairSnapshot(), capture=capture)
        self.assertEqual((f.rule, f.side), (1, -1))
        self.assertTrue(f.change_evidence['applied'])

    def test_monthly_rule1_needs_no_capture(self):
        e, bars, raw, _ = self.setup_flow(timeframe='MN1')
        f = e.step(bars[4], 4, raw, None, bars, PairSnapshot())
        self.assertEqual((f.rule, f.side), (1, -1))
        self.assertFalse(f.rule1_check['requires_higher'])

    def test_same_bar_capture_and_fvg_is_blocked(self):
        e, bars, raw, capture = self.setup_flow()
        capture = replace(capture, captured_at=bars[3].time)
        e.guard.broken, e.guard_broken_at = 3, 3
        f = e.step(bars[4], 4, raw, None, bars, PairSnapshot(), capture=capture)
        self.assertNotEqual(f.rule, 1)
        self.assertIn('opposite_fvg_strictly_after_capture', f.rule1_check['blockers'])

    def test_broken_newest_candidate_leaves_older_zone_available(self):
        e, bars, raw, capture = self.setup_flow()
        older = e.rule_one.candidates[0]
        newest = deepcopy(older)
        newest.created = 4
        newest.zone.broken = 4
        e.rule_one.candidates.append(newest)
        f = e.step(bars[4], 4, raw, None, bars, PairSnapshot(), capture=capture)
        self.assertEqual(f.rule, 1)
        self.assertEqual(f.rule1_check['opposite_fvg']['formed_at'], bars[3].time.isoformat())

    def test_eligible_zone_wins_over_newer_zone_before_capture(self):
        # Equal-time FVG is ineligible; a later formed candidate is selected.
        e, bars, raw, capture = self.setup_flow()
        old = deepcopy(e.rule_one.candidates[0])
        old.created = 2
        e.rule_one.candidates.insert(0, old)
        f = e.step(bars[4], 4, raw, None, bars, PairSnapshot(), capture=capture)
        self.assertEqual(f.rule, 1)
        self.assertEqual(f.rule1_check['opposite_fvg']['formed_at'], bars[3].time.isoformat())


class TestHigherHierarchy(unittest.TestCase):
    def test_only_strictly_higher_sources_are_considered(self):
        for tf, wanted in [('W1', {'MN1'}), ('D1', {'MN1', 'W1'}), ('MN1', set())]:
            bars = [Candle(BASE, 10, 11, 9, 10)]
            a = Analysis(bars, [], [], [], [], tf)
            rows = audit_higher_captures(a, {})
            self.assertEqual(set(rows[0].source_status), wanted)
            self.assertEqual(rows[0].consumer_tf, tf)

    def test_pipeline_passes_monthly_state_to_week_and_day(self):
        bars = [Candle(BASE + timedelta(days=i), 10, 11 + i, 9, 10) for i in range(3)]
        result, parents = analyze_timeframes({'MN1': bars, 'W1': bars, 'D1': bars}, 'D1',
                                            asof=BASE + timedelta(days=90))
        self.assertEqual(len(result.liquidity_audit), 3)
        self.assertEqual(len(parents['W1'].liquidity_audit), 3)


if __name__ == '__main__':
    unittest.main()
