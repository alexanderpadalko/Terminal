"""A delayed feed must not confirm an hour from incomplete delayed prices."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flowdesk.market_cache import MarketService, MarketSnapshot
from flowdesk.market_data import Candle, MOSCOW, aggregate

BASE = datetime(2026, 11, 11, tzinfo=MOSCOW)
HOURS = [Candle(BASE.replace(hour=9), 8.7, 9, 8.527, 8.8),
         Candle(BASE.replace(hour=10), 8.8, 9.306, 8.7, 9.1),
         Candle(BASE.replace(hour=11), 8.579, 8.592, 8.46, 8.513)]
NATIVE = {tf: aggregate(HOURS, tf) for tf in ('D1', 'W1', 'MN1')}
BUNDLE = (HOURS, {'H1': HOURS, **NATIVE}, {})


class TestDelayedFeed(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'cache.sqlite3'
        self.now = BASE.replace(hour=12, minute=5)
        self.service = MarketService(self.path, clock=lambda: self.now)
        calendar = patch('flowdesk.market_cache.load_session_overrides', return_value={})
        calendar.start()
        self.addCleanup(calendar.stop)

    def fetch(self, refresh=False):
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=BUNDLE):
            return self.service.view('AFKS', 'TQBR', 'H1', refresh=refresh)

    def test_hour_waits_until_delayed_data_horizon_reaches_its_close(self):
        before, _, info = self.fetch()
        self.assertEqual(before.asof, BASE.replace(hour=11, minute=50))
        self.assertEqual((before.confirmed_bars, before.flows[-1].side, before.flows[-1].rule), (2, 1, 0))
        self.assertEqual(info['requested_at'], self.now.isoformat())
        self.assertEqual(info['feed_delay_seconds'], 900)
        self.now = BASE.replace(hour=12, minute=15)
        after, _, _ = self.fetch(refresh=True)
        self.assertEqual(after.asof, BASE.replace(hour=12))
        self.assertEqual((after.confirmed_bars, after.flows[-1].side, after.flows[-1].rule), (3, -1, 2))

    def test_restart_does_not_promote_old_delayed_snapshot(self):
        first, _, _ = self.fetch()
        restarted = MarketService(self.path, clock=lambda: BASE.replace(hour=15))
        with patch('flowdesk.market_cache.load_moex_session_bundle') as request:
            result, _, info = restarted.view('AFKS', 'TQBR', 'H1')
        request.assert_not_called()
        self.assertEqual(result.asof, first.asof)
        self.assertEqual(result.confirmed_bars, 2)
        self.assertEqual(info['requested_at'], self.now.isoformat())

    def test_legacy_wall_time_snapshot_is_tightened_once_without_download(self):
        key = self.service.key('AFKS', 'TQBR', {})
        self.service.store.write(key, MarketSnapshot('AFKS', 'TQBR', self.now, HOURS, None, {}, NATIVE))
        with patch('flowdesk.market_cache.load_moex_session_bundle') as request:
            first, _, _ = self.service.view('AFKS', 'TQBR', 'H1')
            second, _, _ = self.service.view('AFKS', 'TQBR', 'H1')
        request.assert_not_called()
        self.assertEqual(first.asof, BASE.replace(hour=11, minute=50))
        self.assertIs(second, first)
        self.assertEqual(first.flows[-1].rule, 0)

    def test_request_timestamp_is_frozen_before_slow_network(self):
        def network(*args, **kwargs):
            self.now = BASE.replace(hour=12, minute=30)
            return BUNDLE
        with patch('flowdesk.market_cache.load_moex_session_bundle', side_effect=network):
            result, _, _ = self.service.view('AFKS', 'TQBR', 'H1')
        self.assertEqual(result.asof, BASE.replace(hour=11, minute=50))
        self.assertEqual(result.flows[-1].rule, 0)


if __name__ == '__main__':
    unittest.main()
