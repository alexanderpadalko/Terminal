"""Rule-1 shadow must preserve flow while reporting strict chronology."""
import unittest
from datetime import datetime, timedelta

from flowdesk.engine import Analysis, FVG, FlowState, PairSnapshot, analyze
from flowdesk.liquidity import Capture, CaptureState
from flowdesk.market_data import Candle, MOSCOW
from flowdesk.rule1_shadow import (
    audit_rule_one_shadow, check_rule_one, serialize_shadow_rule_one,
)

BASE = datetime(2026, 11, 6, 7, tzinfo=MOSCOW)
READY = {"MN1": "READY", "W1": "READY", "D1": "READY"}


def bar(i, high=10, low=9, close=9.7):
    return Candle(BASE + timedelta(hours=i), close, high, low, close, 100)


def capture(i, side=-1):
    return Capture("D1", side, BASE + timedelta(hours=i), "MAX/IDM", 10, False)


def checks(**kw):
    params = dict(side=1, guard_present=True,
                  first_broken_at=BASE + timedelta(hours=2),
                  opposite_fvg={"formed_at": BASE + timedelta(hours=4)},
                  capture=capture(2), all_higher_ready=True, actual_rule=0)
    params.update(kw)
    return check_rule_one(**params)


class TestRuleOneGates(unittest.TestCase):
    def test_exact_equality_of_break_and_capture_allowed(self):
        facts, failures, outcome, would = checks()
        self.assertTrue(all(facts.values()))
        self.assertEqual(failures, ())
        self.assertEqual((outcome, would), ("WOULD_TRIGGER_RULE_1", True))

    def test_first_break_earlier_than_capture_blocks(self):
        facts, fail, outcome, would = checks(
            first_broken_at=BASE + timedelta(hours=1))
        self.assertFalse(facts["break_on_or_after_capture"])
        self.assertIn("break_on_or_after_capture", fail)
        self.assertFalse(would)

    def test_opposite_fvg_same_bar_as_capture_is_too_early(self):
        facts, fail, _, would = checks(
            opposite_fvg={"formed_at": BASE + timedelta(hours=2)})
        self.assertFalse(facts["opposite_fvg_strictly_after_capture"])
        self.assertFalse(would)

    def test_wrong_direction_capture_never_confirms(self):
        facts, fail, _, would = checks(capture=capture(2, side=1))
        self.assertFalse(facts["opposite_liquidity_capture"])
        self.assertFalse(would)

    def test_missing_higher_data_does_not_fabricate_confirmation(self):
        facts, fail, _, would = checks(all_higher_ready=False)
        self.assertIn("higher_sources_ready", fail)
        self.assertFalse(would)

    def test_no_guard_no_fvg_no_capture_produces_all_blockers(self):
        facts, fail, _, would = checks(
            guard_present=False, first_broken_at=None,
            opposite_fvg=None, capture=None)
        self.assertIn("first_fvg_present", fail)
        self.assertIn("first_fvg_broken", fail)
        self.assertIn("opposite_fvg_ready", fail)
        self.assertIn("opposite_liquidity_capture", fail)
        self.assertFalse(would)

    def test_rule_two_overrides_even_if_rule_one_passes(self):
        facts, fail, outcome, would = checks(actual_rule=2)
        self.assertTrue(all(facts.values()))
        self.assertEqual((outcome, would), ("OVERRIDDEN_BY_RULE_2", False))

    def test_rule_one_preempts_three_if_otherwise_eligible(self):
        facts, fail, outcome, would = checks(actual_rule=3)
        self.assertEqual((outcome, would), ("WOULD_PREEMPT_RULE_3", True))


class TestShadowReplay(unittest.TestCase):
    def setUp(self):
        # Five H1 candles generate a descending structure and a bearish FVG.
        self.candles = [
            bar(0, 10.5, 10.0, 10.1),
            bar(1, 10.4, 9.7, 9.8),
            bar(2, 10.0, 9.5, 9.6),
            bar(3, 9.8, 9.2, 9.3),
            bar(4, 9.4, 9.0, 9.1),
        ]
        first_fvg = FVG(0, 0, 0, 10.0, 9.6, 1)
        opposite = FVG(2, 3, 4, 9.5, 9.4, -1)
        self.analysis = Analysis(
            self.candles, [], [first_fvg, opposite],
            [FlowState(i, side=1, lo=9, hi=12, start=0, guard=first_fvg,
                       last_change_at=BASE, last_change_rule=0)
             for i in range(len(self.candles))],
            [PairSnapshot()] * len(self.candles), "H1")
        selected = capture(2)
        self.audit = [
            CaptureState(c.time, c.time + timedelta(hours=1), READY.copy(),
                         selected if i >= 2 else None, 3)
            for i, c in enumerate(self.candles)
        ]

    def test_full_timeline_only_triggers_after_opposite_fvg_formed(self):
        rows = audit_rule_one_shadow(self.analysis, self.audit)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0].outcome, "BLOCKED")
        self.assertFalse(rows[3].would_trigger)
        self.assertEqual((rows[4].outcome, rows[4].would_trigger),
                         ("WOULD_TRIGGER_RULE_1", True))
        event = serialize_shadow_rule_one(rows)["hypothetical_events"][0]
        self.assertEqual(event["first_broken_at"], self.candles[2].time.isoformat())
        self.assertEqual(event["opposite_fvg"]["formed_at"], self.candles[4].time.isoformat())
        self.assertEqual(event["higher_capture"]["captured_at"], self.candles[2].time.isoformat())

    def test_persistent_condition_not_emitted_as_duplicated_event(self):
        extra = bar(5, 9.3, 8.8, 8.9)
        self.analysis.candles.append(extra)
        self.analysis.flows.append(FlowState(5, side=1, lo=9, hi=12, start=0,
                                             guard=self.analysis.fvg[0],
                                             last_change_at=BASE))
        self.analysis.pairs.append(PairSnapshot())
        self.audit.append(CaptureState(extra.time, extra.time + timedelta(hours=1),
                                       READY.copy(), capture(2), 3))
        rows = audit_rule_one_shadow(self.analysis, self.audit)
        self.assertEqual(rows[4].outcome, "WOULD_TRIGGER_RULE_1")
        self.assertEqual(rows[5].outcome, "PERSISTING_AFTER_SHADOW_TRIGGER")
        self.assertFalse(rows[5].would_trigger)
        self.assertEqual(len(serialize_shadow_rule_one(rows)["hypothetical_events"]), 1)

    def test_source_data_not_changed_by_read_only_replay(self):
        before = [(f.side, f.lo, f.hi, f.rule, f.guard) for f in self.analysis.flows]
        audit_rule_one_shadow(self.analysis, self.audit)
        after = [(f.side, f.lo, f.hi, f.rule, f.guard) for f in self.analysis.flows]
        self.assertEqual(before, after)
        self.assertEqual([z.broken for z in self.analysis.fvg], [None, None])

    def test_rule2_priority_on_same_bar(self):
        self.analysis.flows[-1] = FlowState(4, side=-1, rule=2, guard=None)
        rows = audit_rule_one_shadow(self.analysis, self.audit)
        self.assertEqual(rows[-1].outcome, "OVERRIDDEN_BY_RULE_2")
        self.assertFalse(rows[-1].would_trigger)

    def test_one_h1_bar_is_not_published_before_close(self):
        rows = audit_rule_one_shadow(self.analysis, self.audit,
                                     asof=BASE + timedelta(hours=4, minutes=30))
        self.assertEqual(rows[-1].outcome, "H1_NOT_CLOSED")
        self.assertFalse(rows[-1].would_trigger)
        summary = serialize_shadow_rule_one(rows)
        self.assertEqual(summary["latest_closed"]["h1_open"], self.candles[3].time.isoformat())
        self.assertEqual(summary["closed_bars"], 4)

    def test_h1_and_m5_flow_history_identical_with_shadow_audit(self):
        h1 = analyze(self.candles, "H1")
        m5 = [Candle(BASE + timedelta(minutes=i * 5), 9.5, 9.6, 9.4, 9.5, 100)
              for i in range(60)]
        m5_before = analyze(m5, "M5", higher_h1=h1)
        observed = audit_rule_one_shadow(h1, [
            CaptureState(c.time, c.time + timedelta(hours=1), READY.copy(), None, 3)
            for c in self.candles])
        self.assertEqual(len(observed), 5)
        m5_after = analyze(m5, "M5", higher_h1=h1)
        self.assertEqual([(f.side, f.lo, f.hi, f.rule) for f in m5_before.flows],
                         [(f.side, f.lo, f.hi, f.rule) for f in m5_after.flows])

    def test_bad_alignment_never_silently_shifts_events(self):
        wrong = self.audit[:]
        wrong[0] = CaptureState(BASE + timedelta(minutes=5),
                                BASE + timedelta(hours=1), READY.copy(), None, 3)
        with self.assertRaises(ValueError):
            audit_rule_one_shadow(self.analysis, wrong)


if __name__ == "__main__":
    unittest.main()
