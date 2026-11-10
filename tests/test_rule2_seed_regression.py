"""Regression: unguarded Rule 2 must not mask the first new FVG.

Pine flowSeedImpulse is na for Rule 2 without a reversal FVG.
"""
import unittest
from datetime import datetime, timedelta

from flowdesk.engine import FVG, FlowEngine, PairSnapshot, StructurePoint
from flowdesk.market_data import Candle, MOSCOW

BASE = datetime(2026, 11, 5, 20, tzinfo=MOSCOW)


def candle(i, o, h, l, c):
    return Candle(BASE + timedelta(hours=i), o, h, l, c, 100)


class TestRule2SeedRegression(unittest.TestCase):
    def test_first_fvg_after_unguarded_rule2_is_eligible_for_rule3(self):
        bars = [
            candle(0, 8.6, 8.7, 8.5, 8.6),
            candle(1, 8.6, 8.617, 8.527, 8.575),
            candle(2, 8.63, 8.63, 8.63, 8.63),
            candle(3, 8.63, 8.835, 8.61, 8.75),
            candle(4, 8.75, 9.03, 8.747, 9.001),
        ]
        engine = FlowEngine()
        engine.side = -1
        engine.start = 0
        engine.lo, engine.hi = 7.989, 8.7
        engine.lo_i = engine.hi_i = 0
        engine.structural_reverse_lo, engine.structural_reverse_hi = 8.5, 8.7
        engine.structural_reverse_lo_i = engine.structural_reverse_hi_i = 0

        def sp(i, tip):
            return StructurePoint(i, 1, 1, 8.527, i, tip, 0, None, None)

        reversed_flow = engine.step(bars[3], 3, sp(3, 8.835), None, bars, PairSnapshot())
        self.assertEqual((reversed_flow.rule, reversed_flow.side), (2, 1))
        self.assertEqual(reversed_flow.lo, 7.989)
        self.assertIsNone(engine.seed_impulse)

        first_new_fvg = FVG(start=2, impulse=3, created=4, top=8.747, bottom=8.63, side=1)
        flow = engine.step(bars[4], 4, sp(4, 9.03), first_new_fvg, bars, PairSnapshot())
        self.assertEqual(flow.rule, 3)
        self.assertEqual((flow.lo, flow.hi), (8.527, 9.03))
        self.assertEqual(flow.range_low_at, bars[1].time)
        self.assertEqual(flow.last_change_at, bars[4].time)


if __name__ == "__main__":
    unittest.main()
