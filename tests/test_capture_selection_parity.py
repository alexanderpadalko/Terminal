"""Pine parity tests: one active direction, source history and FVG fallback."""
import unittest
from datetime import datetime, timedelta

from flowdesk.engine import Analysis, FVG, FlowState, PairSnapshot, analyze
from flowdesk.liquidity import (Capture, audit_higher_captures,
                                 select_pine_capture, serialize_audit)
from flowdesk.market_data import Candle, MOSCOW

T = datetime(2026, 11, 8, tzinfo=MOSCOW)


def bar(time, hi, lo, close=None):
    return Candle(time, close if close is not None else (hi+lo)/2,
                  hi, lo, close if close is not None else (hi+lo)/2, 100)


class TestCaptureSourceSelection(unittest.TestCase):
    def test_newer_source_supersedes_both_sides_and_tie_prefers_daily(self):
        a = Capture("MN1", -1, T, "MAX/IDM", 9.1, False)
        b = Capture("W1", 1, T, "MIN/IDM", 8.8, False)
        c = Capture("D1", -1, T, "MAX/IDM", 9.2, False)
        self.assertIs(select_pine_capture({"MN1": a, "W1": b}), b)
        self.assertIs(select_pine_capture({"MN1": a, "W1": b, "D1": c}), c)
        later = Capture("W1", 1, T + timedelta(hours=1), "FVG", 8.9, True, 9, 8.9)
        self.assertIs(select_pine_capture({"MN1": a, "W1": later, "D1": c}), later)

    def test_november_8_9_sweep_followed_by_fvg_and_source_close_rollback(self):
        # Nov 8: D1 MAX sweep -> SHORT; Nov 9: D1 bullish FVG entry -> LONG
        # supersedes SHORT. On Nov 10 daily close below FVG, LONG is revoked
        # and prior SHORT becomes source-active again (Pine stack behavior).
        guard = FVG(0, 0, 0, top=9.042, bottom=8.67, side=1, source_tf="D1")
        d1 = [
            bar(T - timedelta(days=1), 9.4, 8.0, 9),
            bar(T, 9.4, 8.0, 9),
            bar(T + timedelta(days=1), 9.4, 8.0, 8.6),
        ]
        flows = [FlowState(0),
                 FlowState(1, side=1, lo=8, hi=10, guard=guard),
                 FlowState(2, side=1, lo=8, hi=10, guard=guard, guard_broken=True)]
        pairs = [PairSnapshot(9.198, 7, 0, 0, 1),
                 PairSnapshot(10.5, 7, 1, 0, 1),
                 PairSnapshot(10.5, 7, 1, 0, 1)]
        source = Analysis(d1, [], [], flows, pairs, "D1")
        h1 = analyze([
            bar(T + timedelta(hours=10), 9.3, 9.0, 9.2),
            bar(T + timedelta(days=1, hours=7), 9.1, 8.9, 9),
            bar(T + timedelta(days=1, hours=13), 9.0, 8.7, 8.83),
            bar(T + timedelta(days=1, hours=23), 9.0, 8.75, 8.9),
        ], "H1")
        states = audit_higher_captures(h1, {"D1": source})
        self.assertEqual((states[0].selected.side, states[0].selected.kind), (-1, "MAX/IDM"))
        self.assertEqual((states[1].selected.side, states[1].selected.kind), (1, "FVG"))
        self.assertEqual([c.side for c in states[2].source_histories["D1"]], [-1, 1])
        self.assertEqual(states[2].selected.side, 1)
        self.assertEqual((states[3].selected.side, states[3].selected.kind), (-1, "MAX/IDM"))
        self.assertEqual(len(states[3].cancelled), 1)
        self.assertEqual(states[3].restored[0].side, -1)
        self.assertEqual(states[3].selection_reason, "RESTORED_AFTER_SOURCE_FVG_CANCEL")
        summary = serialize_audit(states)
        self.assertEqual([s["selected"]["side"] for s in summary["selection_changes"]], [-1, 1, -1])
        self.assertEqual(summary["latest"]["selected_short"]["side"], -1)
        self.assertIsNone(summary["latest"]["selected_long"])

    def test_source_history_read_only_and_unfinished_hour_never_emits(self):
        short = Capture("D1", -1, T, "MAX/IDM", 9.198, False)
        day = Analysis([bar(T - timedelta(days=1), 10, 7)], [], [],
                       [FlowState(0)], [PairSnapshot(9.198, 7, 0, 0, 1)], "D1")
        hours = [bar(T + timedelta(hours=10), 9.3, 9), bar(T + timedelta(hours=11), 9.4, 9)]
        h1 = analyze(hours, "H1")
        before = [(f.side, f.lo, f.hi, f.rule) for f in h1.flows]
        states = audit_higher_captures(h1, {"D1": day},
                                       asof=T + timedelta(hours=11, minutes=20))
        self.assertIsNone(states[1].selected)
        self.assertEqual(states[1].source_histories, {})
        self.assertEqual(before, [(f.side, f.lo, f.hi, f.rule) for f in h1.flows])
        self.assertEqual(len(serialize_audit(states)["selection_changes"]), 1)


if __name__ == "__main__":
    unittest.main()
