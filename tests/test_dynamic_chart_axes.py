"""Adaptive axis interval and market-calendar regressions (no GUI dependency)."""
from datetime import datetime, timedelta, timezone
from math import isclose
from types import SimpleNamespace
import unittest

from flowdesk.chart_axes import nice_price_step, price_axis_ticks, time_axis_ticks
from flowdesk.exchange_time import exchange_clock


def candles(start, count, minutes=5):
    return [SimpleNamespace(time=start + timedelta(minutes=i * minutes))
            for i in range(count)]


class TestDynamicPriceRuler(unittest.TestCase):
    def test_zoom_changes_real_round_price_steps_not_fixed_seven_labels(self):
        short = price_axis_ticks(83400, 83700, 650, .25)
        medium = price_axis_ticks(83000, 84000, 650, .25)
        long = price_axis_ticks(80000, 90000, 650, .25)
        short_step = short[1] - short[0]
        medium_step = medium[1] - medium[0]
        long_step = long[1] - long[0]
        self.assertLess(short_step, medium_step)
        self.assertLess(medium_step, long_step)
        self.assertNotEqual(len(short), 7)
        for values in (short, medium, long):
            self.assertTrue(all(isclose(v / .25, round(v / .25),
                                        abs_tol=1e-7) for v in values))
            self.assertEqual(values, sorted(set(values)))

    def test_ticks_react_to_vertical_size_and_respect_fractional_finam_step(self):
        tall = price_axis_ticks(1.0, 2.0, 900, .025)
        short = price_axis_ticks(1.0, 2.0, 260, .025)
        self.assertGreater(len(tall), len(short))
        self.assertLess(nice_price_step(1.0, 2.0, 900, .025),
                        nice_price_step(1.0, 2.0, 260, .025))
        self.assertTrue(all(isclose(value / .025, round(value / .025),
                                    abs_tol=1e-7) for value in tall + short))
        odd = price_axis_ticks(0, 2, 400, .03)
        self.assertTrue(odd)
        self.assertTrue(all(isclose(value / .03, round(value / .03),
                                    abs_tol=1e-7) for value in odd))
        self.assertEqual(nice_price_step(0, 2, 400, .03), .3)

    def test_zoom_and_pan_preserve_global_round_level_anchors(self):
        before = price_axis_ticks(82200, 83000, 650, 1)
        shifted = price_axis_ticks(82300, 83100, 650, 1)
        self.assertEqual(nice_price_step(82200, 83000, 650, 1),
                         nice_price_step(82300, 83100, 650, 1))
        self.assertEqual(set(before).intersection(shifted),
                         {v for v in before if 82300 <= v <= 83000})
        self.assertEqual(price_axis_ticks(1, 1, 650), [])
        self.assertEqual(price_axis_ticks(float("nan"), 1, 650), [])


class TestDynamicTimeRuler(unittest.TestCase):
    def test_zoom_makes_intermediate_exchange_time_labels_appear(self):
        bars = candles(datetime(2026, 11, 12, 8, tzinfo=timezone.utc), 150)
        local = exchange_clock("XCME", "NQZ26").local
        wide = time_axis_ticks(bars, 0, len(bars), 0, 1.5, local)
        normal = time_axis_ticks(bars, 0, len(bars), 0, 8.0, local)
        zoomed = time_axis_ticks(bars, 0, len(bars), 0, 30.0, local)
        self.assertLess(len(wide), len(normal))
        self.assertLess(len(normal), len(zoomed))
        self.assertTrue(all(0 <= index < len(bars) for seq in
                            (wide, normal, zoomed) for index, _ in seq))
        for stride, seq in ((1.5, wide), (8.0, normal), (30.0, zoomed)):
            self.assertTrue(all((seq[i+1][0] - seq[i][0])*stride >= 93
                                for i in range(len(seq)-1)))

    def test_intraday_label_uses_exchange_time_and_days_at_boundaries(self):
        bars = candles(datetime(2026, 11, 13, 19, tzinfo=timezone.utc),
                       150, 5)
        clock = exchange_clock("XCME", "NQZ26")
        ticks = time_axis_ticks(bars, 0, len(bars), 0, 25, clock.local)
        self.assertTrue(ticks)
        self.assertTrue(any("." in label for _, label in ticks))
        self.assertTrue(any(":" in label for _, label in ticks))
        self.assertTrue(all(index >= 0 and
                            clock.local(bars[index].time).tzinfo == clock.zone
                            for index, _ in ticks))

    def test_dst_repeat_is_two_real_exchange_hours_not_synthetic_time(self):
        # Chicago repeats 01:00 after the first Sunday in November.
        start = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)
        bars = candles(start, 9, 60)
        local = exchange_clock("XCME", "NQZ26").local
        ticks = time_axis_ticks(bars, 0, len(bars), 0, 180, local)
        hours = [label for _, label in ticks]
        self.assertGreaterEqual(hours.count("01:00"), 2)
        self.assertTrue(all(bars[index].time.tzinfo is timezone.utc
                            for index, _ in ticks))

    def test_market_gaps_and_monthly_bars_use_actual_timestamps(self):
        local = exchange_clock("MISX").local
        moments = [
            datetime(2026, 11, 13, 11, tzinfo=timezone.utc),
            datetime(2026, 11, 13, 12, tzinfo=timezone.utc),
            datetime(2026, 11, 16, 8, tzinfo=timezone.utc),
            datetime(2026, 11, 16, 9, tzinfo=timezone.utc),
            datetime(2026, 11, 16, 10, tzinfo=timezone.utc),
            datetime(2026, 11, 17, 8, tzinfo=timezone.utc),
        ]
        bars = [SimpleNamespace(time=value) for value in moments]
        ticks = time_axis_ticks(bars, 0, len(bars), 0, 165, local)
        self.assertTrue(ticks)
        self.assertTrue(all(index < len(bars) for index, _ in ticks))
        self.assertNotIn("15.11", [text for _, text in ticks])
        # Some tiny views have no boundary, but still show one actual candle.
        self.assertEqual(len(time_axis_ticks(bars, 2, 3, 2, 45, local)), 1)
        monthly = [SimpleNamespace(time=datetime(2025 + i//12,
                   i%12+1, 1, tzinfo=timezone.utc)) for i in range(36)]
        labels = time_axis_ticks(monthly, 0, 36, 0, 12, local)
        self.assertTrue(labels)
        self.assertTrue(all("." in label or label.isdigit()
                            for _, label in labels))


if __name__ == "__main__":
    unittest.main()
