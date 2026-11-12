"""Regression: a provisional close cannot change a published flow."""
import unittest
from datetime import datetime, timedelta, timezone

from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW, candle_close_time
from flowdesk.pipeline import analyze_timeframes, source_coverage


def bars_at(times):
    # A long structural leg followed by a close through its lower boundary.
    values = [(9.5, 10, 9, 9.5), (9.8, 11, 9.3, 10.8), (9.5, 10, 8.6, 8.8)]
    return [Candle(t, *v, 100) for t, v in zip(times, values)]


class TestConfirmedFlows(unittest.TestCase):
    def test_provisional_rule_two_waits_for_close_on_every_flow_timeframe(self):
        cases = {
            "H1": [datetime(2026, 11, 11, h, tzinfo=MOSCOW) for h in (9, 10, 11)],
            "D1": [datetime(2026, 11, d, tzinfo=MOSCOW) for d in (9, 10, 11)],
            "W1": [datetime(2026, 11, d, tzinfo=MOSCOW) for d in (2, 9, 16)],
            "MN1": [datetime(2026, m, 1, tzinfo=MOSCOW) for m in (9, 10, 11)],
        }
        for tf, times in cases.items():
            with self.subTest(tf=tf):
                candles = bars_at(times)
                end = candle_close_time(candles[-1], tf)
                live = analyze(candles, tf, asof=end - timedelta(seconds=1))
                self.assertEqual((live.flows[-1].side, live.flows[-1].rule), (1, 0))
                self.assertEqual(live.flows[-1].lo, live.flows[-2].lo)
                self.assertEqual(live.flows[-1].last_change_at, live.flows[-2].last_change_at)
                self.assertEqual(live.confirmed_bars, 2)
                self.assertEqual(live.flows[-1].source_bar_time, times[1])
                closed = analyze(candles, tf, asof=end)
                self.assertEqual((closed.flows[-1].side, closed.flows[-1].rule), (-1, 2))
                self.assertEqual(closed.flows[-1].source_confirmed_at, end)

    def test_afks_partial_close_below_8527_does_not_reverse(self):
        # Only the last OHLCV is from the user's 11:16 diagnostic. The two
        # setup bars establish the boundary; this is not a full-history parity test.
        day = datetime(2026, 11, 11, tzinfo=MOSCOW)
        candles = [Candle(day.replace(hour=9), 8.7, 9.0, 8.527, 8.8),
                   Candle(day.replace(hour=10), 8.8, 9.306, 8.7, 9.1),
                   Candle(day.replace(hour=11), 8.579, 8.592, 8.523, 8.524, 1824700)]
        live = analyze(candles, "H1", asof=day.replace(hour=11, minute=16, second=37))
        self.assertEqual((live.flows[-1].side, live.flows[-1].lo, live.flows[-1].hi),
                         (1, 8.527, 9.306))
        self.assertEqual(live.flows[-1].rule, 0)
        self.assertEqual(analyze(candles, "H1").flows[-1].rule, 2)

    def test_same_origin_live_wick_can_extend_without_new_event(self):
        base = datetime(2026, 11, 11, 9, tzinfo=MOSCOW)
        candles = bars_at([base + timedelta(hours=i) for i in range(3)])
        candles[-1] = Candle(base + timedelta(hours=2), 10.8, 12, 10, 11.7)
        live = analyze(candles, "H1", asof=base + timedelta(hours=2, minutes=15))
        self.assertEqual((live.flows[-1].side, live.flows[-1].rule), (1, 0))
        self.assertEqual(live.flows[-1].hi, 12)
        self.assertEqual(live.flows[-2].hi, 11)
        self.assertTrue(live.flows[-1].live_range)

    def test_live_fvg_break_cannot_mutate_confirmed_projection(self):
        base = datetime(2026, 11, 1, tzinfo=MOSCOW)
        values = [(9, 10, 8, 9), (11, 12, 9.5, 11.5),
                  (12, 13, 10.5, 12.5), (12, 12.5, 9, 9.9)]
        candles = [Candle(base + timedelta(days=i), *v) for i, v in enumerate(values)]
        live = analyze(candles, "D1", asof=base + timedelta(days=3, hours=12))
        self.assertEqual(live.fvg[0].broken, 3)
        self.assertIsNone(live.published_fvg[0].broken)
        self.assertIsNot(live.fvg[0], live.published_fvg[0])
        self.assertIsNone(live.flows[2].guard.broken)

    def test_latest_historical_fvg_is_published_when_its_bar_is_closed(self):
        base = datetime(2026, 11, 1, tzinfo=MOSCOW)
        candles = [Candle(base, 9, 10, 8, 9),
                   Candle(base + timedelta(days=1), 11, 12, 9.5, 11.5),
                   Candle(base + timedelta(days=2), 12, 13, 10.5, 12.5)]
        a = analyze(candles, "D1", asof=base + timedelta(days=3))
        self.assertEqual(len(a.published_fvg), 1)
        self.assertEqual(a.published_fvg[0].created, len(candles) - 1)

    def test_no_confirmed_history_does_not_publish_first_flow(self):
        base = datetime(2026, 11, 11, 11, tzinfo=MOSCOW)
        a = analyze([Candle(base, 9, 10, 8, 9)], "H1", asof=base)
        self.assertEqual(a.confirmed_bars, 0)
        self.assertEqual((a.flows[0].side, a.flows[0].rule), (0, 0))
        self.assertIsNone(a.flows[0].source_confirmed_at)

    def test_parent_snapshot_cannot_unlock_beyond_its_own_asof(self):
        base = datetime(2026, 11, 11, 9, tzinfo=MOSCOW)
        candles = bars_at([base + timedelta(hours=i) for i in range(3)])
        parent = analyze(candles, "H1", asof=base + timedelta(hours=2, minutes=15))
        lower = [Candle(base + timedelta(hours=2, minutes=55), 9, 10, 8, 9)]
        a = analyze(lower, "M5", higher_h1=parent)
        self.assertEqual(a.flows[-1].source_bar_time, base + timedelta(hours=1))
        self.assertEqual(a.flows[-1].side, 1)

    def test_naive_calculation_time_rejected(self):
        with self.assertRaisesRegex(ValueError, "часовой пояс"):
            analyze([], "H1", asof=datetime(2026, 11, 11))


class TestSourceCoverage(unittest.TestCase):
    def test_full_h1_history_used_on_native_and_lower_chart(self):
        base = datetime(2025, 1, 1, tzinfo=MOSCOW)
        hourly = [Candle(base + timedelta(hours=i), 9, 10, 8, 9) for i in range(3005)]
        lower = [Candle(hourly[-1].time + timedelta(minutes=55), 9, 10, 8, 9)]
        at = hourly[-1].time + timedelta(hours=1)
        native, _ = analyze_timeframes({"H1": hourly}, "H1", asof=at)
        _, parents = analyze_timeframes({"H1": hourly, "M5": lower}, "M5", asof=at)
        self.assertEqual(len(native.candles), 3005)
        self.assertEqual(len(parents["H1"].candles), 3005)
        self.assertEqual(native.flows[-1].lo, parents["H1"].flows[-1].lo)

    def test_long_monthly_csv_and_individual_provenance_are_retained(self):
        months = [Candle(datetime(2010 + i // 12, i % 12 + 1, 1, tzinfo=MOSCOW),
                         9, 10, 8, 9) for i in range(203)]
        hourly = [Candle(datetime(2026, 11, 11, 11, tzinfo=MOSCOW), 9, 10, 8, 9)]
        at = datetime(2026, 11, 11, 11, 16, tzinfo=MOSCOW)
        provenance = {"MN1": {"mode": "USER_CSV", "file": "AFKS_M.csv"},
                      "H1": {"higher_from_clean_h1": True}}
        main, parents = analyze_timeframes({"H1": hourly, "MN1": months}, "H1",
                                           asof=at, provenance=provenance)
        coverage = source_coverage(parents["MN1"])
        self.assertEqual((coverage["bars"], coverage["confirmed_bars"]), (203, 202))
        self.assertFalse(coverage["history_complete"])
        self.assertEqual(main.asof, parents["MN1"].asof)
        self.assertEqual(parents["MN1"].session_info["mode"], "USER_CSV")
        self.assertTrue(main.session_info["higher_from_clean_h1"])


class TestCloseTime(unittest.TestCase):
    def test_utc_csv_day_uses_moscow_calendar_and_leap_month(self):
        c = Candle(datetime(2024, 2, 29, 4, tzinfo=timezone.utc), 9, 10, 8, 9)
        expected = datetime(2024, 3, 1, tzinfo=MOSCOW)
        self.assertEqual(candle_close_time(c, "D1"), expected)
        self.assertEqual(candle_close_time(c, "MN1"), expected)

    def test_week_and_december_rollover(self):
        c = Candle(datetime(2026, 12, 31, 7, tzinfo=MOSCOW), 9, 10, 8, 9)
        self.assertEqual(candle_close_time(c, "MN1"), datetime(2027, 1, 1, tzinfo=MOSCOW))
        self.assertEqual(candle_close_time(c, "W1"), datetime(2027, 1, 4, tzinfo=MOSCOW))


if __name__ == "__main__":
    unittest.main()
