"""2024–26 MOEX TQBR session/holiday calendar, trading-date grouping and FVG."""
import json
import tempfile
import unittest
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

from flowdesk.engine import analyze
from flowdesk.market_data import (aggregate, exclude_opening_auction,
                                  opening_auction_window, load_session_overrides,
                                  load_moex_session_bundle)
from flowdesk.moex_calendar import (REGULAR, EXTRA, CLOSED, SessionOverride,
                                    stock_session_kind, stock_trading_date)
from test_session_clean_all_timeframes import bar


class TestPinnedTradingCalendar(unittest.TestCase):
    def test_2026_weekday_holidays_use_weekend_auction_not_6am(self):
        expected = {date(2026, 2, 23): date(2026, 2, 24),
                    date(2026, 5, 1): date(2026, 5, 4),
                    date(2026, 6, 12): date(2026, 6, 15),
                    date(2026, 11, 4): date(2026, 11, 5)}
        for day, tradedate in expected.items():
            with self.subTest(day=day):
                self.assertEqual(stock_session_kind(day), EXTRA)
                self.assertEqual(opening_auction_window(day),
                                 (time(9, 50), time(10)))
                self.assertEqual(stock_trading_date(day), tradedate)
                cleaned = exclude_opening_auction([
                    bar(day, 9, o=30, hi=30, lo=30, close=30),
                    bar(day, 10, o=10, hi=11, lo=9, close=10)], "H1")
                self.assertEqual([c.time.hour for c in cleaned], [10])

    def test_calendar_exceptions_closed_and_regular_sessions(self):
        examples = {
            date(2026, 1, 3): CLOSED, date(2026, 5, 9): CLOSED,
            date(2026, 11, 7): EXTRA, date(2026, 11, 9): REGULAR,
            date(2025, 1, 26): CLOSED, date(2025, 3, 1): EXTRA,
            date(2025, 3, 8): CLOSED, date(2025, 11, 1): REGULAR,
            date(2024, 11, 2): REGULAR, date(2024, 11, 3): CLOSED,
        }
        for day, kind in examples.items():
            with self.subTest(day=day):
                self.assertEqual(stock_session_kind(day), kind)
        self.assertEqual(opening_auction_window(date(2025, 11, 1)),
                         (time(6, 50), time(7)))
        self.assertEqual(opening_auction_window(date(2024, 11, 2)),
                         (time(9, 50), time(10)))
        with self.assertRaisesRegex(ValueError, "неторговый день"):
            exclude_opening_auction([bar(date(2026, 5, 9), 9)], "H1")

    def test_extra_session_aggregation_skips_holidays_and_crosses_week(self):
        # 1 May 2026 Friday is additional session, not regular trade date;
        # 2/3 May are also additional. 4 May is the owning REGULAR day.
        holiday, sat, sun, monday = (date(2026, 5, d) for d in (1, 2, 3, 4))
        raw = [bar(holiday, 9, o=50, hi=50, lo=50, close=50, vol=200),
               bar(holiday, 10, o=10, hi=11, lo=9, close=10, vol=10),
               bar(sat, 9, o=50, hi=50, lo=50, close=50, vol=200),
               bar(sat, 10, o=10, hi=12, lo=9, close=11, vol=20),
               bar(sun, 10, o=11, hi=13, lo=10, close=12, vol=30),
               bar(monday, 7, o=12, hi=14, lo=11, close=13, vol=40)]
        clean = exclude_opening_auction(raw, "H1")
        self.assertEqual(len(clean), 4)
        for tf in ("D1", "W1", "MN1"):
            result = aggregate(clean, tf, moex_trading_day=True)
            self.assertEqual(len(result), 1, tf)
            self.assertEqual((result[0].open, result[0].high, result[0].low,
                              result[0].close, result[0].volume),
                             (10, 14, 9, 13, 100), tf)
            self.assertEqual(result[0].time.date(),
                             monday if tf == "D1" else
                             date(2026, 5, 4) if tf == "W1" else date(2026, 5, 1))

    def test_november_special_weekday_is_folded_into_thursday(self):
        h, d = date(2026, 11, 4), date(2026, 11, 5)
        clean = exclude_opening_auction([
            bar(h, 9, o=50, hi=50, lo=50, close=50, vol=1000),
            bar(h, 10, o=8.0, hi=8.2, lo=7.9, close=8.1, vol=300),
            bar(d, 7, o=8.1, hi=8.3, lo=8.0, close=8.2, vol=400),
        ], "H1")
        daily = aggregate(clean, "D1", moex_trading_day=True)
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0].time.date(), d)
        self.assertEqual((daily[0].high, daily[0].volume), (8.3, 700))

    def test_sunday_month_end_migrates_to_next_month(self):
        a, b = date(2026, 10, 31), date(2026, 11, 1)
        self.assertEqual(stock_trading_date(a), date(2026, 11, 2))
        self.assertEqual(stock_trading_date(b), date(2026, 11, 2))
        m = aggregate([bar(a, 10), bar(b, 10)], "MN1", moex_trading_day=True)
        self.assertEqual([x.time.date() for x in m], [date(2026, 11, 1)])

    def test_legacy_and_full_overrides_remain_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "auction_windows.json"
            cfg.write_text(json.dumps({
                "2027-02-06": {"kind": "EXTRA", "auction": ["08:50", "09:00"],
                               "trading_date": "2027-02-08"},
                "2026-12-12": ["08:50", "09:00"],
                "2026-12-13": None,
            }), encoding="utf-8")
            loaded = load_session_overrides(cfg)
            d = date(2027, 2, 6)
            self.assertIsInstance(loaded[d], SessionOverride)
            self.assertEqual(stock_session_kind(d, loaded), EXTRA)
            self.assertEqual(stock_trading_date(d, loaded), date(2027, 2, 8))
            self.assertEqual(opening_auction_window(d, overrides=loaded),
                             (time(8, 50), time(9)))
            self.assertEqual(opening_auction_window(date(2026, 12, 12), overrides=loaded),
                             (time(8, 50), time(9)))
            self.assertIsNone(opening_auction_window(date(2026, 12, 13),
                                                     overrides=loaded))

    def test_minute_five_minute_holiday_auction_removed(self):
        holiday = date(2026, 11, 4)
        minutes = [bar(holiday, 9, minute=50, o=50, hi=50, lo=50, close=50),
                   bar(holiday, 9, minute=59, o=50, hi=50, lo=50, close=50),
                   bar(holiday, 10, minute=0, o=10, hi=11, lo=9, close=10),
                   bar(holiday, 10, minute=1, o=10, hi=12, lo=8, close=11)]
        clean = exclude_opening_auction(minutes, "M1")
        self.assertEqual(len(clean), 2)
        five = aggregate(clean, "M5")
        self.assertEqual((len(five), five[0].high), (1, 12))

    def test_bundle_provenance_explains_holiday_mapping(self):
        h = date(2026, 11, 4)
        raw = [bar(h, 9, o=50, hi=50, lo=50, close=50),
               bar(h, 10, o=10, hi=12, lo=9, close=11)]
        with patch('flowdesk.market_data._iss_download', return_value=raw):
            visible, higher, meta = load_moex_session_bundle('AFKS', 'H1')
        self.assertEqual(len(visible), 1)
        self.assertEqual(meta['auction_h1_removed'], 1)
        self.assertEqual(meta['holiday_extra_sessions_removed'], ['2026-11-04'])
        self.assertEqual(meta['session_trade_date_mapping'], [
            {'physical_date': '2026-11-04', 'trade_date': '2026-11-05'}])
        # Chart candles stay on the physical date; the settlement date is
        # provided separately as metadata, not used to aggregate OHLCV.
        self.assertEqual(higher['D1'][0].time.date(), date(2026, 11, 4))
        self.assertEqual(meta['higher_bar_grouping'],
                         'PHYSICAL_MOSCOW_CALENDAR_DATE_FROM_CLEAN_H1')
        self.assertEqual(meta['exchange_trading_date_usage'],
                         'SETTLEMENT_METADATA_ONLY_NOT_BAR_GROUPING')


if __name__ == "__main__":
    unittest.main()
