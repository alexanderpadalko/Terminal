"""HTTP permission: no silent fetch, even for missing/cold data."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from flowdesk.market_cache import MarketService
from flowdesk.market_data import Candle, MOSCOW, aggregate
from flowdesk.settings import ApplicationSettings, IndicatorSettings, SettingsStore

BASE = datetime(2026, 11, 11, 12, tzinfo=MOSCOW)


class TestHttpPermission(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.service = MarketService(Path(self.folder.name)/'cached.sqlite3',
                                     clock=lambda: BASE, load_metadata=True)
        self.sessions = patch('flowdesk.market_cache.load_session_overrides', return_value={})
        self.sessions.start()
        self.addCleanup(self.sessions.stop)

    def test_no_http_cold_start(self):
        with patch.object(self.service, '_fetch') as fetch, \
             patch('flowdesk.market_data.security_tick') as tick:
            with self.assertRaisesRegex(RuntimeError, 'HTTP выключен'):
                self.service.view('SBER', 'TQBR', 'H1', allow_http=False)
        fetch.assert_not_called()
        tick.assert_not_called()

    def test_cache_only_mode_never_refreshes_http_or_metadata(self):
        hours = [Candle(BASE.replace(hour=9), 10, 12, 9, 11),
                 Candle(BASE.replace(hour=10), 11, 13, 10, 12),
                 Candle(BASE.replace(hour=11), 12, 14, 11, 13)]
        native = {tf: aggregate(hours, tf) for tf in ('D1','W1','MN1')}
        bundle = (hours, {'H1': hours, **native}, {})
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle), \
             patch('flowdesk.market_data.security_tick', return_value=.01):
            self.service.view('SBER', 'TQBR', 'H1', allow_http=True)
        with patch.object(self.service, '_fetch') as fetch, \
             patch('flowdesk.market_data.security_tick') as tick:
            data, _, info = self.service.view('SBER', 'TQBR', 'H1', refresh=True, allow_http=False)
        fetch.assert_not_called()
        tick.assert_not_called()
        self.assertEqual(info['tier'], 'memory')
        self.assertEqual(data.candles, hours)
        self.assertIn('HTTP выключен', info['warning'])

    def test_permission_defaults_off_and_is_persistent(self):
        self.assertFalse(ApplicationSettings().allow_http)
        store = SettingsStore(Path(self.folder.name)/'settings.json')
        store.write(IndicatorSettings(), ApplicationSettings(allow_http=True))
        self.assertTrue(store.read()[1].allow_http)
        store.write(IndicatorSettings(), ApplicationSettings())
        self.assertFalse(store.read()[1].allow_http)


if __name__ == '__main__':
    unittest.main()
