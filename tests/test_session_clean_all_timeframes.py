"""Opening-auction source-bar removal must precede ALL analyses and resampling."""
import unittest
import tempfile
from pathlib import Path
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from flowdesk.engine import analyze
from flowdesk.market_data import (
    Candle, MOSCOW, aggregate, download_moex, exclude_opening_auction,
    load_moex_session_bundle, opening_auction_window, load_session_overrides,
)


def bar(day, hour, minute=0, o=10, hi=11, lo=9, close=10.5, vol=100):
    return Candle(datetime(day.year, day.month, day.day, hour, minute, tzinfo=MOSCOW),
                  o, hi, lo, close, vol)


class TestAuctionWindow(unittest.TestCase):
    def test_moex_change_2025_and_2026_weekday_weekend(self):
        self.assertEqual(opening_auction_window(date(2024, 10, 18)),
                         (time(9, 50), time(10, 0)))
        self.assertEqual(opening_auction_window(date(2025, 1, 26)),
                         (time(9, 50), time(10, 0)))
        self.assertEqual(opening_auction_window(date(2025, 1, 27)),
                         (time(6, 50), time(7, 0)))
        self.assertEqual(opening_auction_window(date(2026, 11, 7)),
                         (time(9, 50), time(10, 0)))

    def test_date_override_can_move_auction_without_code_changes(self):
        d = date(2026, 12, 12)
        overrides = {d: (time(8, 50), time(9, 0))}
        self.assertEqual(opening_auction_window(d, overrides=overrides),
                         (time(8, 50), time(9, 0)))
        candles = [bar(d, 8, o=100, hi=100, lo=100, close=100), bar(d, 9)]
        self.assertEqual([c.time.hour for c in exclude_opening_auction(
            candles, 'H1', overrides=overrides)], [9])

    def test_date_override_file_without_changing_code(self):
        with tempfile.TemporaryDirectory() as tempdir:
            src = Path(tempdir) / "auction_windows.json"
            src.write_text('{"2026-12-12": ["08:50", "09:00"], "2026-12-13": null}',
                           encoding="utf-8")
            schedule = load_session_overrides(src)
            self.assertEqual(opening_auction_window(date(2026, 12, 12),
                                                    overrides=schedule),
                             (time(8, 50), time(9, 0)))
            self.assertIsNone(opening_auction_window(date(2026, 12, 13),
                                                     overrides=schedule))

    def test_non_tqbr_profile_must_not_guess(self):
        with self.assertRaises(ValueError):
            exclude_opening_auction([bar(date(2026, 11, 9), 6)], 'H1', board='TQOB')

    def test_weekday_2026_removes_0600_but_keeps_0900(self):
        d = date(2026, 11, 9)
        candles = [bar(d, 6, o=9.208, hi=9.208, lo=9.208, close=9.208),
                   bar(d, 7), bar(d, 9), bar(d, 10)]
        keep = exclude_opening_auction(candles, 'H1')
        self.assertEqual([c.time.hour for c in keep], [7, 9, 10])
        self.assertEqual(len(exclude_opening_auction(keep, 'H1')), len(keep))

    def test_weekend_removes_0900_but_keeps_1000(self):
        d = date(2026, 11, 8)
        bars = [bar(d, 9), bar(d, 10), bar(d, 11)]
        self.assertEqual([c.time.hour for c in exclude_opening_auction(bars, 'H1')],
                         [10, 11])

    def test_pre_morning_session_2024_removes_weekday_0900(self):
        d = date(2024, 10, 18)
        bars = [bar(d, 9), bar(d, 10), bar(d, 11)]
        self.assertEqual([c.time.hour for c in exclude_opening_auction(bars, 'H1')],
                         [10, 11])

    def test_zero_range_regular_candle_not_filtered(self):
        d = date(2026, 11, 9)
        bars = [bar(d, 7, o=100, hi=100, lo=100, close=100),
                bar(d, 9, o=100, hi=100, lo=100, close=100)]
        self.assertEqual(len(exclude_opening_auction(bars, 'H1')), 2)

    def test_m1_and_m5_remove_only_auction_minutes(self):
        weekday, weekend = date(2026, 11, 9), date(2026, 11, 8)
        minute = [bar(weekday, 6, m) for m in [49, 50, 59]] + [bar(weekday, 7)]
        minute += [bar(weekend, 9, m) for m in [49, 50, 59]] + [bar(weekend, 10)]
        actual = exclude_opening_auction(minute, 'M1')
        self.assertEqual([(b.time.day, b.time.hour, b.time.minute) for b in actual],
                         [(9, 6, 49), (9, 7, 0), (8, 9, 49), (8, 10, 0)])
        five = [bar(weekday, 6, 45), bar(weekday, 6, 50), bar(weekday, 6, 55),
                bar(weekday, 7), bar(weekend, 9, 50), bar(weekend, 9, 55),
                bar(weekend, 10)]
        self.assertEqual([(b.time.day, b.time.hour, b.time.minute) for b in
                          exclude_opening_auction(five, 'M5')],
                         [(9, 6, 45), (9, 7, 0), (8, 10, 0)])

    def test_refuse_partial_auction_contaminating_h1(self):
        d = date(2026, 12, 12)
        custom = {d: (time(9, 10), time(9, 20))}
        with self.assertRaisesRegex(ValueError, 'нужен M1'):
            exclude_opening_auction([bar(d, 9)], 'H1', overrides=custom)


class TestAllTimeframesRebuilt(unittest.TestCase):
    def test_weekend_stock_session_is_next_trading_day_even_at_month_boundary(self):
        sat, sun, mon = date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)
        bars = [bar(sat, 10, close=9), bar(sun, 10, close=10), bar(mon, 7, close=11)]
        d = aggregate(bars, 'D1', moex_trading_day=True)
        w = aggregate(bars, 'W1', moex_trading_day=True)
        m = aggregate(bars, 'MN1', moex_trading_day=True)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0].time.date(), mon)
        self.assertEqual((len(w), w[0].time.date()), (1, mon))
        self.assertEqual((len(m), m[0].time.date()), (1, date(2026, 11, 1)))
        # Generic/TradingView CSV aggregation is unchanged and non-destructive.
        self.assertEqual(len(aggregate(bars, 'D1')), 3)

    def test_day_week_month_exclude_auction_extreme_and_volume(self):
        d = date(2026, 11, 9)  # Monday and November
        raw = [bar(d, 6, o=50, hi=50, lo=50, close=50, vol=1000),
               bar(d, 7, o=10, hi=12, lo=8, close=10, vol=300),
               bar(d, 8, o=10, hi=13, lo=9, close=11, vol=400)]
        filtered = exclude_opening_auction(raw, 'H1')
        for tf in ('D1', 'W1', 'MN1'):
            packed = aggregate(filtered, tf)
            self.assertEqual(len(packed), 1)
            self.assertEqual((packed[0].open, packed[0].high,
                              packed[0].low, packed[0].close, packed[0].volume),
                             (10, 13, 8, 11, 700))
        self.assertEqual([s.time.hour for s in filtered], [7, 8])

    def test_m5_rebuilt_from_minute_without_auction_price(self):
        d = date(2026, 11, 9)
        raw = [bar(d, 6, 50, o=50, hi=50, lo=50, close=50),
               bar(d, 6, 59, o=50, hi=50, lo=50, close=50),
               bar(d, 7, 0, o=10, hi=11, lo=9, close=10),
               bar(d, 7, 1, o=10, hi=12, lo=8, close=11)]
        five = aggregate(exclude_opening_auction(raw, 'M1'), 'M5')
        self.assertEqual(len(five), 1)
        self.assertEqual((five[0].high, five[0].low), (12, 8))

    def test_fvg_checks_adjacent_retained_bars_not_auction_middle(self):
        prev = date(2026, 11, 8)
        next_day = date(2026, 11, 9)
        bars = [bar(prev, 18, o=9.27, hi=9.277, lo=9.230, close=9.243),
                bar(next_day, 6, o=9.208, hi=9.208, lo=9.208, close=9.208),
                bar(next_day, 7, o=9.205, hi=9.208, lo=8.977, close=9.036),
                bar(next_day, 8, o=9.036, hi=9.065, lo=8.893, close=8.934)]
        clean = exclude_opening_auction(bars, 'H1')
        self.assertEqual([b.time.hour for b in clean], [18, 7, 8])
        output = analyze(clean, 'H1')
        self.assertFalse(any(z.bottom == 9.065 and z.top == 9.208 for z in output.fvg))

    def test_download_moex_higher_rebuilds_from_hourly_not_native_daily(self):
        d = date(2026, 11, 9)
        raw = [bar(d, 6, o=50, hi=50, lo=50, close=50),
               bar(d, 7, o=10, hi=11, lo=9, close=10)]
        with patch('flowdesk.market_data._iss_download', return_value=raw) as mocked:
            result = download_moex('AFKS', 'D1', days=1)
        self.assertEqual(mocked.call_args.args[1], 60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].high, 11)

    def test_session_bundle_single_hourly_source_all_tfs(self):
        d = date(2026, 11, 9)
        raw = [bar(d, 6, o=50, hi=50, lo=50, close=50),
               bar(d, 7, o=10, hi=11, lo=9, close=10),
               bar(d, 8, o=10, hi=12, lo=9, close=11)]
        with patch('flowdesk.market_data._iss_download', return_value=raw) as mocked:
            displayed, sources, provenance = load_moex_session_bundle('AFKS', 'H1')
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(len(displayed), 2)
        self.assertEqual(set(sources), {'H1', 'D1', 'W1', 'MN1'})
        self.assertEqual({tf: sources[tf][0].high for tf in ('D1', 'W1', 'MN1')},
                         {'D1': 12, 'W1': 12, 'MN1': 12})
        self.assertTrue(provenance['higher_from_clean_h1'])


if __name__ == '__main__':
    unittest.main()
