"""Session H4 aggregation, closed-bar publication and existing-TF isolation."""
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flowdesk.engine import analyze
from flowdesk.liquidity import HIGHER_SOURCES
from flowdesk.market_cache import MarketService
from flowdesk.market_data import (Candle, MOSCOW, aggregate, aggregate_moex_h4,
                                  candle_close_time, infer_timeframe, serialize_candle)
from flowdesk.pipeline import analyze_timeframes


def hour(day, h, value=10):
    return Candle(datetime(2026, 11, day, h, tzinfo=MOSCOW), value, value + 2, value - 1, value + 1, 100)


class TestH4(unittest.TestCase):
    def test_regular_session_anchors_at_seven_and_keeps_ohlcv(self):
        source = [hour(11, h, h) for h in range(7, 24)]
        bars = aggregate_moex_h4(source)
        self.assertEqual([b.time.hour for b in bars], [7, 11, 15, 19, 23])
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume),
                         (7, 12, 6, 11, 400))
        self.assertEqual(candle_close_time(bars[0], 'H4'), source[0].time.replace(hour=11))
        self.assertEqual(candle_close_time(bars[-1], 'H4'), source[0].time.replace(hour=0) + timedelta(days=1))
        self.assertEqual(sum(b.volume for b in bars), sum(b.volume for b in source))

    def test_extra_session_starts_at_ten_and_final_bar_closes_at_nineteen(self):
        bars = aggregate_moex_h4([hour(8, h) for h in range(10, 19)])
        self.assertEqual([b.time.hour for b in bars], [10, 14, 18])
        self.assertEqual(candle_close_time(bars[-1], 'H4').hour, 19)
        asof = bars[-1].time.replace(hour=18, minute=59)
        self.assertEqual(analyze(bars, 'H4', asof=asof).confirmed_bars, 2)
        self.assertEqual(analyze(bars, 'H4', asof=asof.replace(hour=19, minute=0)).confirmed_bars, 3)

    def test_partial_history_uses_session_anchor_not_first_downloaded_hour(self):
        bars = aggregate_moex_h4([hour(11, 9), hour(11, 10)])
        self.assertEqual(bars[0].time.hour, 7)
        self.assertEqual(candle_close_time(bars[0], 'H4').hour, 11)
        partial = aggregate_moex_h4([hour(11, 11)])
        self.assertEqual(candle_close_time(partial[0], 'H4').hour, 15)
        self.assertEqual(analyze(partial, 'H4', asof=hour(11, 12).time).confirmed_bars, 0)

    def test_no_cross_day_merging_or_empty_bar_fabrication(self):
        bars = aggregate_moex_h4([hour(11, 23), hour(12, 7)])
        self.assertEqual([(b.time.day, b.time.hour, b.volume) for b in bars], [(11, 23, 100), (12, 7, 100)])
        self.assertEqual(candle_close_time(bars[0], 'H4'), hour(12, 0).time)

    def test_scheduled_close_survives_diagnostic_export(self):
        bar = aggregate_moex_h4([hour(11, 23)])[0]
        payload = serialize_candle(bar)
        self.assertEqual(datetime.fromisoformat(payload['scheduled_close']), candle_close_time(bar, 'H4'))
        self.assertNotIn('scheduled_close', serialize_candle(hour(11, 7)))
        self.assertEqual(infer_timeframe([hour(11, h) for h in (7, 11, 15, 19, 23)]), 'H4')

    def test_rule_two_waits_for_h4_close(self):
        base = hour(11, 7).time
        bars = [Candle(base, 9, 10, 8, 9),
                Candle(base + timedelta(hours=4), 9, 11, 9, 10),
                Candle(base + timedelta(hours=8), 9, 10, 7, 7.5)]
        before = analyze(bars, 'H4', asof=base + timedelta(hours=11, minutes=59))
        after = analyze(bars, 'H4', asof=base + timedelta(hours=12))
        self.assertEqual((before.flows[-1].side, before.flows[-1].rule), (1, 0))
        self.assertEqual((after.flows[-1].side, after.flows[-1].rule), (-1, 2))

    def test_h4_uses_daily_weekly_monthly_without_changing_h1_hierarchy(self):
        self.assertEqual(HIGHER_SOURCES['H4'], ('MN1', 'W1', 'D1'))
        self.assertEqual(HIGHER_SOURCES['H1'], ('MN1', 'W1', 'D1'))

    def test_h4_does_not_change_existing_flow_histories(self):
        hours = [hour(day, h, 10 + ((day + h) % 5)) for day in (9, 10, 11) for h in range(7, 24)]
        minutes = [Candle(hours[0].time + timedelta(minutes=i), 10, 11, 9, 10) for i in range(180)]
        series = {'H1': hours, 'M1': minutes, 'M5': aggregate(minutes, 'M5'),
                  **{tf: aggregate(hours, tf) for tf in ('D1', 'W1', 'MN1')}}
        asof = hour(12, 0).time
        for tf in ('M1', 'M5', 'H1', 'D1', 'W1', 'MN1'):
            with self.subTest(tf=tf):
                before, _ = analyze_timeframes(series, tf, asof=asof)
                after, _ = analyze_timeframes({**series, 'H4': aggregate_moex_h4(hours)}, tf, asof=asof)
                self.assertEqual([asdict(f) for f in before.flows], [asdict(f) for f in after.flows])

    def test_cached_h4_reuses_hours_and_restores_fixed_close_time(self):
        hours = [hour(11, h) for h in range(7, 13)]
        asof = hour(11, 12).time.replace(minute=30)
        metadata = {'higher_from_clean_h1': True}
        bundle = (hours, {'H1': hours, **{tf: aggregate(hours, tf) for tf in ('D1', 'W1', 'MN1')}}, metadata)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite3'
            service = MarketService(path, clock=lambda: asof)
            with patch('flowdesk.market_cache.load_session_overrides', return_value={}), \
                 patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle) as network:
                service.view('AFKS', 'TQBR', 'H1')
                h4, _, _ = service.view('AFKS', 'TQBR', 'H4')
            self.assertEqual(network.call_count, 1)
            self.assertEqual((len(h4.candles), h4.confirmed_bars), (2, 1))
            self.assertEqual(candle_close_time(h4.candles[-1], 'H4').hour, 15)
            later = MarketService(path, clock=lambda: asof + timedelta(days=1))
            with patch('flowdesk.market_cache.load_session_overrides', return_value={}), \
                 patch('flowdesk.market_cache.load_moex_session_bundle') as network:
                restored, _, info = later.view('AFKS', 'TQBR', 'H4')
            network.assert_not_called()
            self.assertEqual(restored.candles, h4.candles)
            self.assertEqual(restored.confirmed_bars, 1)
            self.assertEqual(info['asof'], asof.isoformat())


if __name__ == '__main__':
    unittest.main()
