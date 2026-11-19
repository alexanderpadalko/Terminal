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
        self.assertEqual([call.args[2:] for call in read.call_args_list], [('M5', latest.time.isoformat())])
        self.assertIs(initial['M5'][0], updated['M5'][0])
        self.assertEqual(updated['M5'][-1], latest)
        self.assertEqual(initial['M5'][-1], self.bars[-1])
        self.now += timedelta(minutes=5)
        self.assertFalse(self.market._upsert(self.symbol, 'M5', [latest], source='stream'))
        self.assertIn(('M5', latest.time.isoformat(), self.now.isoformat()),
                      self.chart_source().source_state['observations'])

    def test_older_correction_and_coverage_and_archive_progress(self):
        self.chart_source()
        corrected = replace(self.bars[7], close=102)
        self.market._upsert(self.symbol, 'M5', [corrected])
        session_store.record_coverage(self.market, self.symbol, 'M5', corrected.time, self.now)
        self.market.save_history_progress(self.symbol, 'FULL_ARCHIVE_v2_M5',
                                          {'cursor': corrected.time, 'exhausted': False})
        updated = self.chart_source()
        self.assertTrue(updated.source_state['full_archive'])
        self.assertEqual(updated['M5'][7], corrected)

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
        with patch.object(chart_source_cache, 'MAX_ROWS', 1):
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
