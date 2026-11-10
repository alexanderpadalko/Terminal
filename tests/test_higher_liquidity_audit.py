"""Rule-1 H1 preview: confirm time gating and conservative source events."""
import unittest
from datetime import datetime, timedelta

from flowdesk.engine import Analysis, FVG, FlowState, PairSnapshot, analyze
from flowdesk.liquidity import audit_higher_captures, period_end, serialize_audit
from flowdesk.market_data import Candle, MOSCOW

DAY = datetime(2026, 11, 3, tzinfo=MOSCOW)


def candle(at, lo=9.0, hi=10.0, close=9.5):
    return Candle(at, close, hi, lo, close, 100)


def h1(*bars):
    return analyze(list(bars), "H1")


def src_d1(*bars, pairs=None, flows=None):
    pairs = pairs or [PairSnapshot(11, 9, 0, 0, 1) for _ in bars]
    flows = flows or [FlowState(i) for i in range(len(bars))]
    return Analysis(list(bars), [], [], flows, pairs, "D1")


class TestHigherLiquidityAudit(unittest.TestCase):
    def test_period_end_month_and_week(self):
        self.assertEqual(period_end(candle(datetime(2026, 12, 1, tzinfo=MOSCOW)), "MN1"),
                         datetime(2027, 1, 1, tzinfo=MOSCOW))
        self.assertEqual(period_end(candle(datetime(2026, 11, 2, tzinfo=MOSCOW)), "W1"),
                         datetime(2026, 11, 9, tzinfo=MOSCOW))

    def test_unavailable_higher_source_has_no_fake_capture(self):
        lower = h1(candle(DAY + timedelta(hours=10), 8, 12),
                   candle(DAY + timedelta(hours=11), 8, 12))
        audits = audit_higher_captures(lower, {})
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(a.selected is None and a.available_sources == 0 for a in audits))
        self.assertEqual(len(serialize_audit(audits)["events"]), 0)

    def test_no_intraday_access_to_incomplete_daily_candle(self):
        higher = src_d1(candle(DAY, 7, 12))  # current D1 bar: closes at Nov 4 00:00
        lower = h1(candle(DAY + timedelta(hours=10), 8.5, 11.1))
        a = audit_higher_captures(lower, {"D1": higher})[0]
        self.assertIsNone(a.selected)
        self.assertEqual(a.source_status["D1"], "AWAIT_HIGHER_CLOSE")

    def test_only_strict_sweep_and_once_per_pair_key(self):
        higher = src_d1(candle(DAY - timedelta(days=1), 9, 11))
        lower = h1(candle(DAY + timedelta(hours=10), 9.0, 11.0),
                   candle(DAY + timedelta(hours=11), 8.9, 10.5),
                   candle(DAY + timedelta(hours=12), 8.8, 10.5))
        a = audit_higher_captures(lower, {"D1": higher})
        self.assertIsNone(a[0].selected)  # touches alone do not sweep
        self.assertEqual((a[1].selected.side, a[1].selected.kind), (1, "MIN/IDM"))
        self.assertEqual(a[1].selected.captured_at, DAY + timedelta(hours=11))
        self.assertEqual(len(a[2].new_events), 0)  # same key already captured

    def test_both_directions_on_one_h1_bar_cancel(self):
        higher = src_d1(candle(DAY - timedelta(days=1), 9, 11))
        lower = h1(candle(DAY + timedelta(hours=10), 8.9, 11.1))
        a = audit_higher_captures(lower, {"D1": higher})[0]
        self.assertIn("D1", a.conflicts)
        self.assertIsNone(a.selected)
        self.assertFalse(a.new_events)

    def test_fvg_capture_cancelled_by_next_closed_source_candle(self):
        # On D1 source: an open bullish working FVG from previous daily bar.
        guard = FVG(0, 0, 0, top=9.0, bottom=8.0, side=1, source_tf="D1")
        higher = src_d1(
            candle(DAY - timedelta(days=1), 7.5, 12, 10),
            candle(DAY, 7.5, 11.5, 7.9),
            pairs=[PairSnapshot(13, 7, 0, 0, 1)] * 2,
            flows=[FlowState(0, side=1, lo=6, hi=12, guard=guard),
                   FlowState(1, side=1, lo=6, hi=12, guard=guard, guard_broken=True)],
        )
        # H1 captures FVG at 10:00, but the D1 candle completed at 00:00
        # Nov 4 closes below FVG and cancels this capture.
        hourly = [candle(DAY + timedelta(hours=i), 8.5 if i in (10, 23) else 9.6,
                         10, 9.8) for i in range(10, 24)]
        lower = h1(*hourly)
        a = audit_higher_captures(lower, {"D1": higher})
        self.assertEqual((a[0].selected.kind, a[0].selected.side), ("FVG", 1))
        self.assertIsNone(a[-1].selected)
        self.assertEqual(len(a[-1].new_events), 0)

    def test_diagnostic_is_read_only_for_h1_flow_and_m5_projection(self):
        hourly = [candle(DAY + timedelta(hours=i), 8.4, 11.3, 10) for i in range(5)]
        first = analyze(hourly, "H1")
        with_audit = analyze(hourly, "H1", higher_liquidity={"D1": src_d1(candle(DAY - timedelta(days=1), 9, 11))})
        self.assertEqual([(f.side, f.lo, f.hi, f.rule) for f in first.flows],
                         [(f.side, f.lo, f.hi, f.rule) for f in with_audit.flows])
        self.assertEqual(len(with_audit.liquidity_audit), len(hourly))
        lower = [candle(DAY + timedelta(minutes=i*5), 8.4, 11.3, 10) for i in range(60)]
        from_audit = analyze(lower, "M5", higher_h1=with_audit)
        base = analyze(lower, "M5", higher_h1=first)
        self.assertEqual([(f.side, f.lo, f.hi, f.rule) for f in from_audit.flows],
                         [(f.side, f.lo, f.hi, f.rule) for f in base.flows])

    def test_asof_never_unlocks_h1_before_close(self):
        higher = src_d1(candle(DAY - timedelta(days=1), 9, 11))
        lower = h1(candle(DAY + timedelta(hours=10), 8.5, 10))
        a = audit_higher_captures(lower, {"D1": higher},
                                  asof=DAY + timedelta(hours=10, minutes=30))[0]
        self.assertIsNone(a.selected)
        self.assertEqual(a.source_status["D1"], "H1_NOT_CLOSED")


if __name__ == "__main__":
    unittest.main()
