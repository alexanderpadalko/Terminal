from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flowdesk import chart_source_cache, session_store
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle


class ChartSourceCacheTests(unittest.TestCase):
    symbol = 'SBER@MISX'

    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.now = datetime(2026, 11, 18, 14, 20, tzinfo=timezone.utc)
        self.market = FinamMarketService(Path(folder) / 'source.sqlite',
            api=SimpleNamespace(session_candles=True, market_delay_minutes=lambda _: 0),
            clock=lambda: self.now)
        self.market._chart_sources.select(self.symbol)
        self.bars = [Candle(self.now - timedelta(minutes=5 * i), 100, 104, 99, 101, 1)
                     for i in range(70, -1, -1)]
        self.market._upsert(self.symbol, 'M5', self.bars)
        self.market._upsert(self.symbol, 'H1', self.bars[:1])

    def chart_source(self):
        cached = self.market._series(self.symbol, chart=True)
        full = self.market._series(self.symbol)
        self.assertEqual(cached, full)
        self.assertEqual({**cached.source_state, 'observations': sorted(cached.source_state['observations'])},
                         {**full.source_state, 'observations': sorted(full.source_state['observations'])})
        return cached

    def test_stream_tail_reuses_prefix_and_observation_only_change_is_visible(self):
        initial = self.chart_source()
        latest = replace(self.bars[-1], close=103, volume=2)
        self.market._upsert(self.symbol, 'M5', [latest], source='stream')
        with patch.object(chart_source_cache, '_read_frame', wraps=chart_source_cache._read_frame) as read:
            updated = self.chart_source()
        self.assertEqual([call.args[2:] for call in read.call_args_list],
                         [('M5', latest.time.isoformat(), latest.time.isoformat())])
        self.assertIs(initial['M5'][0], updated['M5'][0])
        self.assertEqual(updated['M5'][-1], latest)
        self.assertEqual(initial['M5'][-1], self.bars[-1])
        self.now += timedelta(minutes=5)
        self.assertFalse(self.market._upsert(self.symbol, 'M5', [latest], source='stream'))
        self.assertIn(('M5', latest.time.isoformat(), self.now.isoformat()),
                      self.chart_source().source_state['observations'])

    def test_older_correction_and_coverage_and_archive_progress(self):
        initial = self.chart_source()
        corrected = replace(self.bars[7], close=102)
        latest = replace(self.bars[-1], close=103)
        self.market._upsert(self.symbol, 'M5', [corrected])
        self.market._upsert(self.symbol, 'M5', [latest])
        session_store.record_coverage(self.market, self.symbol, 'M5', corrected.time, self.now)
        self.market.save_history_progress(self.symbol, 'FULL_ARCHIVE_v2_M5',
                                          {'cursor': corrected.time, 'exhausted': False})
        with patch.object(chart_source_cache, '_read_frame', wraps=chart_source_cache._read_frame) as read:
            updated = self.chart_source()
        self.assertEqual([call.args[2:] for call in read.call_args_list], [
            ('M5', corrected.time.isoformat(), corrected.time.isoformat()),
            ('M5', latest.time.isoformat(), latest.time.isoformat()),
        ])
        self.assertTrue(updated.source_state['full_archive'])
        self.assertEqual(updated['M5'][7], corrected)
        self.assertIs(initial['M5'][8], updated['M5'][8])

    def test_packed_observations_fit_existing_budget_and_reuse_unaffected_chunks(self):
        with patch.object(chart_source_cache, 'MAX_CACHE_BYTES', 30_240), \
                patch.object(chart_source_cache, '_CHUNK_ROWS', 16):
            initial = self.chart_source()
            first_chunks = self.market._chart_sources.snapshot[1]['M5'][1].chunks
            latest = replace(self.bars[-1], close=103)
            self.market._upsert(self.symbol, 'M5', [latest])
            updated = self.chart_source()
            updated_chunks = self.market._chart_sources.snapshot[1]['M5'][1].chunks
        self.assertIs(first_chunks[0], updated_chunks[0])
        self.assertIs(initial['M5'][0], updated['M5'][0])

    def test_native_closes_share_next_open_and_fall_under_sber_sized_byte_budget(self):
        native = [replace(bar, scheduled_close=bar.time + timedelta(minutes=5)) for bar in self.bars]
        self.market._upsert(self.symbol, 'M5', native)
        cached = self.chart_source()
        self.assertIs(cached['M5'][0].scheduled_close, cached['M5'][1].time)
        self.assertEqual(cached['M5'], native)
        frames = self.market._chart_sources.snapshot[1]
        bars, receipts, _ = frames['M5']
        retained = bars.retained_bytes() + receipts.size
        sber_rows = 647_944
        self.assertLess(retained * sber_rows / len(bars), chart_source_cache.MAX_CACHE_BYTES)

    def test_legacy_orphan_observation_is_read_losslessly(self):
        orphan = ('M5', '2026-11-18T09:00:00.000001+03:01:30', self.now.isoformat())
        with closing(self.market._connection()) as db, db:
            db.execute('INSERT INTO candle_source_observations VALUES (?, ?, ?, ?)', (self.symbol, *orphan))
        original = self.chart_source()
        self.assertIn(orphan, original.source_state['observations'])
        updated = replace(self.bars[-1], close=103)
        self.market._upsert(self.symbol, 'M5', [updated])
        self.assertIn(orphan, self.chart_source().source_state['observations'])

    def test_other_service_revision_and_legacy_offset_invalidate_suffix(self):
        self.chart_source()
        other = FinamMarketService(self.market.path, api=self.market.api, clock=lambda: self.now)
        local_time = self.bars[1].time.astimezone(timezone(timedelta(hours=3)))
        corrected = replace(self.bars[1], time=local_time, close=103)
        other._upsert(self.symbol, 'M5', [corrected])
        self.assertEqual(self.chart_source()['M5'][1], corrected)
        self.market._upsert(self.symbol, 'M5', [replace(self.bars[-1], close=102)])
        with patch.object(chart_source_cache, '_read_frame', wraps=chart_source_cache._read_frame) as read:
            self.chart_source()
        self.assertEqual([call.args[2:] for call in read.call_args_list], [('M5', None)])

    def test_scan_has_its_own_http_suffix(self):
        latest = self.bars[-1]
        self.chart_source()
        with self.market.http_scan():
            self.market._scan_local.snapshot[(self.symbol, 'M5')] = (latest.time, (replace(latest, close=103),))
            self.assertEqual(self.market._series(self.symbol, chart=True)['M5'][-1].close, 103)
        self.assertEqual(self.chart_source()['M5'][-1], latest)

    def test_missing_revision_chain_reloads_and_budget_drops_oversized_snapshot(self):
        self.chart_source()
        with closing(self.market._connection()) as db, db:
            db.execute('UPDATE candle_source_revisions SET revision=revision+99 WHERE symbol=?', (self.symbol,))
        with patch.object(chart_source_cache, 'MAX_CACHE_BYTES', 216):
            self.chart_source()
        self.assertIsNone(self.market._chart_sources.snapshot)

    def test_interrupted_view_does_not_publish_partial_sources(self):
        cancelled = Event()

        def interrupt_after_reading(*args, **kwargs):
            cancelled.set()
            return Candle(*args, **kwargs)

        with patch.object(chart_source_cache, 'Candle', side_effect=interrupt_after_reading), \
                patch.object(self.market, 'prepare_series', wraps=self.market.prepare_series) as prepare:
            with self.assertRaises(InterruptedError):
                self.market.view('SBER', 'MISX', 'M5', cancelled=cancelled.is_set)
            prepare.assert_not_called()
        self.assertIsNone(self.market._chart_sources.snapshot)
        self.assertEqual(self.market._series(self.symbol)['M5'], self.bars)

        analysis, _, _ = self.market.view('SBER', 'MISX', 'M5')
        self.assertEqual(analysis.candles[-1], self.bars[-1])
