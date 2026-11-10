"""Contract tests: lower timeframes display ONLY closed H1 flow snapshots."""
import unittest
from datetime import datetime, timedelta, timezone

from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW

BASE = datetime(2026, 6, 1, 10, tzinfo=MOSCOW)


def hbar(i, high, low, close):
    return Candle(BASE + timedelta(hours=i), close, high, low, close, 100)


def lower_bars(tf, hours=5):
    minutes = 5 if tf == "M5" else 1
    return [Candle(BASE + timedelta(minutes=i * minutes), 9, 10, 8, 9, 10)
            for i in range(hours * 60 // minutes)]


class TestH1FlowProjection(unittest.TestCase):
    def setUp(self):
        self.parent = analyze([
            hbar(0, 10, 8, 9),
            hbar(1, 11, 9.3, 10.8),
            hbar(2, 10, 7.5, 7.8),
            hbar(3, 9.5, 8, 8.5),
        ], "H1")
        self.assertEqual(self.parent.flows[2].rule, 2)
        self.assertEqual(self.parent.flows[2].side, -1)

    def test_m5_inherits_h1_after_hour_close(self):
        candles = lower_bars("M5")
        a = analyze(candles, "M5", higher_h1=self.parent)
        # 12:50 M5 candle closes 12:55, last completed H1 is 11:00.
        i_before = next(i for i,c in enumerate(candles) if c.time.hour == 12 and c.time.minute == 50)
        i_after = i_before + 1   # 12:55 M5 candle closes 13:00
        self.assertEqual(a.flows[i_before].side, 1)
        self.assertEqual(a.flows[i_before].rule, 0)
        self.assertEqual(a.flows[i_after].side, -1)
        self.assertEqual(a.flows[i_after].rule, 2)
        self.assertEqual(a.flows[i_after].source_bar_time, BASE + timedelta(hours=2))
        self.assertEqual(a.flows[i_after].source_confirmed_at, BASE + timedelta(hours=3))
        self.assertEqual(a.flows[i_after + 1].rule, 0)
        self.assertEqual(sum(f.rule == 2 for f in a.flows), 1)
        self.assertEqual(a.flow_source_tf, "H1")
        self.assertEqual(a.flow_status, "H1_CONFIRMED_CANDLES")

    def test_m1_inherits_with_same_h1_event(self):
        candles = lower_bars("M1")
        a = analyze(candles, "M1", higher_h1=self.parent)
        event = [(c.time, f.rule, f.side) for c,f in zip(candles,a.flows) if f.rule == 2]
        self.assertEqual(event, [(BASE + timedelta(hours=2, minutes=59), 2, -1)])

    def test_missing_h1_does_not_compute_local_flow(self):
        candles = lower_bars("M5")
        a = analyze(candles, "M5")
        self.assertEqual(a.flow_status, "H1_UNAVAILABLE")
        self.assertTrue(all(f.side == 0 and f.rule == 0 for f in a.flows))
        self.assertEqual(len(a.flows), len(candles))

    def test_unfinished_h1_not_unlocked_by_nominal_m5_close(self):
        candles = lower_bars("M5")
        a = analyze(candles, "M5", higher_h1=self.parent,
                    asof=BASE + timedelta(hours=2, minutes=45))
        # Even a future-dated candle in the series must not unlock the 12:00 H1,
        # which only confirms at 13:00.
        self.assertTrue(all(f.rule == 0 for f in a.flows))
        self.assertEqual(a.flows[-1].side, 1)

    def test_no_historic_event_replayed_at_start_of_partial_data(self):
        candles = [c for c in lower_bars("M5") if c.time >= BASE + timedelta(hours=3, minutes=30)]
        a = analyze(candles, "M5", higher_h1=self.parent)
        self.assertEqual(a.flows[0].side, -1)
        self.assertEqual(a.flows[0].rule, 0)
        self.assertEqual(sum(f.rule == 2 for f in a.flows), 0)

    def test_previous_and_start_indexes_are_lower_tf_indexes(self):
        candles = lower_bars("M5")
        a = analyze(candles, "M5", higher_h1=self.parent)
        i = next(i for i,c in enumerate(candles) if c.time == BASE + timedelta(hours=2, minutes=55))
        flow = a.flows[i]
        self.assertIsNotNone(flow.previous)
        self.assertLess(flow.start, len(candles))
        self.assertLess(flow.previous[1], len(candles))

    def test_requires_h1_source(self):
        with self.assertRaises(ValueError):
            analyze(lower_bars("M5"), "M5", higher_h1=analyze(lower_bars("M5"), "M5"))

    def test_h1_itself_keeps_preliminary_local_rule(self):
        self.assertEqual(self.parent.flow_source_tf, "H1")
        self.assertEqual(self.parent.flows[2].rule, 2)


if __name__ == "__main__":
    unittest.main()
