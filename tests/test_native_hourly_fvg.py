"""Regression: hourly FVG is forbidden when any one of three bars is inside."""
import unittest
from datetime import datetime, timedelta, timezone

from flowdesk.engine import FVGEngine
from flowdesk.market_data import Candle


BASE = datetime(2026, 11, 18, tzinfo=timezone.utc)


def bars(bounds):
    return [
        Candle(BASE + timedelta(hours=index), low + 1, high, low, high - 1, 10,
               scheduled_close=BASE + timedelta(hours=index + 1))
        for index, (high, low) in enumerate(bounds)
    ]


def zones(bounds, frame="H1", *, exclude_gaps=False):
    detector = FVGEngine(frame, exclude_gaps=exclude_gaps)
    made = []
    for candle in bars(bounds):
        event = detector.step(candle)
        if event is not None:
            made.append(event)
    return made


class NativeHourlyFvgInsideTests(unittest.TestCase):
    def test_clean_three_bar_bullish_fvg_is_accepted(self):
        sample = [(110, 100), (105, 95), (120, 100), (130, 106)]
        made = zones(sample)
        self.assertEqual([(item.start, item.created, item.side) for item in made],
                         [(1, 3, 1)])

    def test_any_inside_candle_in_three_bar_window_forbids_h1_fvg(self):
        # First, second and third candidate candle are inside the most recent
        # external mother bar, respectively. Without the hourly guard the
        # first-to-third bullish price gap satisfies the FVG price inequality.
        examples = {
            "first": [(120, 80), (105, 95), (115, 98), (130, 106)],
            "second": [(90, 80), (105, 95), (103, 96), (125, 106)],
            "third": [(110, 100), (105, 95), (120, 99), (118, 106)],
        }
        for position, source in examples.items():
            with self.subTest(position=position):
                self.assertFalse(zones(source, exclude_gaps=False))
                # The price inequality itself is present; the H1 inside
                # restriction is what rejects this otherwise valid pattern.
                daily = zones(source, "D1", exclude_gaps=False)
                self.assertTrue(any(event.start == 1 and event.created == 3
                                    for event in daily))

    def test_consecutive_inside_bars_use_latest_external_mother(self):
        source = [(120, 80), (105, 95), (110, 97), (130, 106)]
        self.assertFalse(zones(source, exclude_gaps=False))


if __name__ == "__main__":
    unittest.main()
