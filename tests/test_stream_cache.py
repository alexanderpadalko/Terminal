import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flowdesk.iss_plus import FeedEvent
from flowdesk.market_cache import MarketService, MarketSnapshot, PUBLIC_FEED_DELAY
from flowdesk.market_data import Candle, MOSCOW, aggregate
from flowdesk.pipeline import analyze_timeframes
from flowdesk.stream_cache import StreamGap, merge_stream

BASE = datetime(2026, 11, 11, 11, tzinfo=MOSCOW)


def candle(t, value=10, volume=1):
    return Candle(t, 10, max(12, value), 9, value, volume)


def event(tf, bars, at=None):
    return FeedEvent('candles', 'SBER', 'TQBR', 1, at or BASE + timedelta(hours=2), tf, tuple(bars))


def snapshot():
    hours = [candle(BASE - timedelta(hours=2)), candle(BASE - timedelta(hours=1)), candle(BASE)]
    minutes = [candle(BASE + timedelta(minutes=i)) for i in range(30)]
    higher = {tf: aggregate(hours, tf) for tf in ('D1', 'W1', 'MN1')}
    return MarketSnapshot('SBER', 'TQBR', BASE + timedelta(minutes=30), hours, minutes,
                          {'feed_delay_seconds': 900}, higher)


class TestStreamCache(unittest.TestCase):
    def test_upsert_deduplicates_preserves_archive_and_persists(self):
        saved = snapshot()
        with tempfile.TemporaryDirectory() as directory, patch('flowdesk.market_cache.load_session_overrides', return_value={}):
            service = MarketService(Path(directory) / 'cache.sqlite3')
            key = service.key('SBER', 'TQBR', {})
            service.store.write(key, saved)
            service.memory[key] = saved
            bar = candle(BASE + timedelta(minutes=30), 11, 2)
            with patch('flowdesk.market_cache.load_moex_session_bundle') as http:
                service.ingest_stream('SBER', 'TQBR', [event('M1', [bar, bar])])
                service.ingest_stream('SBER', 'TQBR', [event('M1', [bar])])
                a, _, _ = service.view('SBER', 'TQBR', 'M1')
            http.assert_not_called()
            self.assertEqual(len(a.candles), 31)
            disk = service.store.read(key)
            self.assertEqual(disk.minutes[-1], bar)
            self.assertEqual(disk.hours[0], saved.hours[0])
            self.assertEqual(disk.hours[-1].volume, 32)

    def test_ready_minutes_do_not_confirm_derived_hour_before_native(self):
        saved = snapshot()
        additions = [candle(BASE + timedelta(minutes=i)) for i in range(30, 60)]
        merged, _ = merge_stream(saved, [event('M1', additions)], {})
        self.assertLess(merged.asof, BASE + timedelta(hours=1))
        self.assertTrue(merged.metadata['stream_repair_required'])
        final = candle(BASE, 11, 60)
        done, _ = merge_stream(merged, [event('H1', [final])], {})
        self.assertEqual(done.asof, BASE + timedelta(hours=1))
        self.assertEqual(done.hours[-1], final)
        self.assertFalse(done.metadata['stream_repair_required'])

    def test_live_day_does_not_advance_cutoff_or_invalidate_hour(self):
        saved = snapshot()
        main, parents = analyze_timeframes(saved.sources({}), 'M1', asof=saved.asof)
        saved.analyses = {'M1': main, **parents}
        day = replace(saved.native_higher['D1'][-1], close=11)
        merged, _ = merge_stream(saved, [event('D1', [day], BASE + timedelta(hours=10))], {})
        self.assertEqual(merged.asof, saved.asof)
        self.assertIs(merged.analyses['H1'], parents['H1'])
        self.assertNotIn('D1', merged.analyses)

    def test_closed_parent_correction_invalidates_dependents(self):
        saved = snapshot()
        previous = candle(BASE.replace(hour=0) - timedelta(days=1))
        saved.native_higher['D1'].insert(0, previous)
        saved.analyses = {'H1': object(), 'M1': object()}
        merged, _ = merge_stream(saved, [event('D1', [replace(previous, close=11)])], {})
        self.assertNotIn('H1', merged.analyses)
        self.assertNotIn('M1', merged.analyses)

    def test_gaps_rejected_without_mutating_previous_snapshot(self):
        saved = snapshot()
        with self.assertRaises(StreamGap):
            merge_stream(saved, [event('M1', [candle(BASE + timedelta(minutes=40))])], {})
        self.assertEqual(len(saved.minutes), 30)
        self.assertNotIn('stream_received_at', saved.metadata)

    def test_old_http_overlap_and_wrong_instrument_ignored(self):
        saved = snapshot()
        saved.metadata['http_completed_at'] = (BASE + timedelta(hours=3)).isoformat()
        merged, _ = merge_stream(saved, [event('M1', [candle(BASE + timedelta(minutes=30))])], {})
        self.assertIs(merged, saved)
        merged, _ = merge_stream(saved, [replace(event('D1', saved.native_higher['D1']), symbol='AFKS')], {})
        self.assertIs(merged, saved)

    def test_invalid_ohlc_rejected(self):
        saved = snapshot()
        with self.assertRaises(ValueError):
            merge_stream(saved, [event('M1', [replace(candle(BASE), close=100)])], {})

    def test_later_native_hour_cannot_seal_missing_earlier_partial_hour(self):
        saved = snapshot()
        later_hour = candle(BASE + timedelta(hours=1))
        merged, _ = merge_stream(saved, [event('H1', [later_hour], BASE + timedelta(hours=3))], {})
        self.assertLess(merged.asof, BASE + timedelta(hours=1))
        self.assertTrue(merged.metadata['stream_repair_required'])

    def test_stream_replay_matches_fresh_replay_on_all_timeframes(self):
        saved = snapshot()
        initial, parents = analyze_timeframes(saved.sources({}), 'M1', asof=saved.asof)
        saved.analyses = {'M1': initial, **parents}
        additions = [candle(BASE + timedelta(minutes=i), 10 + i % 3 / 10) for i in range(30, 60)]
        merged, _ = merge_stream(saved, [event('M1', additions), event('H1', [candle(BASE, 10.2, 60)])], {})
        cached, cached_parents = analyze_timeframes(merged.sources({}), 'M1', asof=merged.asof, cached=merged.analyses)
        fresh, fresh_parents = analyze_timeframes(merged.sources({}), 'M1', asof=merged.asof)
        for tf, a in {'M1': cached, **cached_parents}.items():
            b = {'M1': fresh, **fresh_parents}[tf]
            self.assertEqual(a.flows, b.flows, tf)
            self.assertEqual(a.fvg, b.fvg, tf)
            self.assertEqual(a.pairs, b.pairs, tf)
            self.assertEqual(a.confirmed_bars, b.confirmed_bars, tf)
