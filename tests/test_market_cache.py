import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flowdesk.market_cache import MarketService
from flowdesk.market_data import Candle, MOSCOW, aggregate


BASE = datetime(2026, 11, 11, tzinfo=MOSCOW)
HOURS = [Candle(BASE.replace(hour=9), 8.7, 9, 8.527, 8.8),
         Candle(BASE.replace(hour=10), 8.8, 9.306, 8.7, 9.1),
         Candle(BASE.replace(hour=11), 8.579, 8.592, 8.46, 8.513)]


def bundle(hours=HOURS):
    return hours, {'H1': hours, **{t: aggregate(hours, t) for t in ('D1', 'W1', 'MN1')}}, {
        'higher_from_clean_h1': True, 'raw_h1_bars': len(hours), 'auction_h1_removed': 0}


class TestMarketCache(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'market.sqlite3'
        self.now = BASE.replace(hour=11, minute=16)
        self.service = MarketService(self.path, clock=lambda: self.now)
        self.sessions = patch('flowdesk.market_cache.load_session_overrides', return_value={})
        self.sessions.start()
        self.addCleanup(self.sessions.stop)

    def seed(self):
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle()):
            return self.service.view('AFKS', 'TQBR', 'H1')

    def test_tf_switch_reuses_archive_and_ready_analysis(self):
        first, parents, _ = self.seed()
        with patch('flowdesk.market_cache.load_moex_session_bundle') as network:
            daily, _, info = self.service.view('AFKS', 'TQBR', 'D1')
            hourly, _, _ = self.service.view('AFKS', 'TQBR', 'H1')
        network.assert_not_called()
        self.assertIs(daily, parents['D1'])
        self.assertIs(hourly, first)
        self.assertEqual(info['tier'], 'memory')

    def test_saved_forming_hour_does_not_confirm_when_wall_clock_advances(self):
        first, _, _ = self.seed()
        self.now = BASE.replace(hour=12, minute=30)
        cached, _, info = self.service.view('AFKS', 'TQBR', 'H1')
        self.assertEqual(cached.asof, BASE.replace(hour=11, minute=16))
        self.assertEqual((cached.flows[-1].side, cached.flows[-1].rule), (1, 0))
        self.assertEqual(info['asof'], first.asof.isoformat())

    def test_restart_restores_observation_time_and_history_without_network(self):
        self.seed()
        later = MarketService(self.path, clock=lambda: BASE.replace(hour=15))
        with patch('flowdesk.market_cache.load_moex_session_bundle') as network:
            restored, _, info = later.view('AFKS', 'TQBR', 'H1')
        network.assert_not_called()
        self.assertEqual(info['tier'], 'disk')
        self.assertEqual(restored.candles, HOURS)
        self.assertEqual((restored.confirmed_bars, restored.flows[-1].side), (2, 1))

    def test_explicit_refresh_merges_overlap_and_keeps_earliest_history(self):
        self.seed()
        self.now = BASE.replace(hour=12, minute=30)
        revised = Candle(HOURS[-1].time, 8.579, 8.592, 8.46, 8.5)
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle([revised])) as network:
            result, _, info = self.service.view('AFKS', 'TQBR', 'H1', refresh=True)
        self.assertEqual(network.call_args.kwargs['history_days'], 2)
        self.assertEqual(result.candles, HOURS[:-1] + [revised])
        self.assertEqual((result.flows[-1].side, result.flows[-1].rule), (-1, 2))
        self.assertEqual(info['tier'], 'network')

    def test_failed_refresh_keeps_previous_coherent_snapshot(self):
        self.seed()
        self.now = BASE.replace(hour=15)
        with patch('flowdesk.market_cache.load_moex_session_bundle', side_effect=ConnectionError('offline')):
            result, _, info = self.service.view('AFKS', 'TQBR', 'H1', refresh=True)
        self.assertEqual(result.asof, BASE.replace(hour=11, minute=16))
        self.assertEqual(result.flows[-1].side, 1)
        self.assertEqual(info['tier'], 'offline')
        self.assertIn('offline', info['warning'])

    def test_m1_and_m5_share_one_download_and_keep_full_h1(self):
        self.seed()
        minutes = [Candle(BASE.replace(hour=11) + timedelta(minutes=i), 8.6, 8.7, 8.5, 8.6)
                   for i in range(60)]
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle(HOURS[-1:])), \
             patch('flowdesk.market_cache._iss_download', return_value=minutes) as request:
            m5, parents, _ = self.service.view('AFKS', 'TQBR', 'M5')
            m1, _, _ = self.service.view('AFKS', 'TQBR', 'M1')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(m1.candles), 60)
        self.assertEqual(m5.candles, aggregate(minutes, 'M5'))
        self.assertEqual(parents['H1'].candles, HOURS)

    def test_failed_minute_fetch_does_not_publish_partial_hour_refresh(self):
        first, _, _ = self.seed()
        self.now = BASE.replace(hour=13)
        revised = Candle(HOURS[-1].time, 8.579, 8.592, 8.46, 8.5)
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle([revised])), \
             patch('flowdesk.market_cache._iss_download', side_effect=ConnectionError('minute fetch failed')):
            with self.assertRaises(ConnectionError):
                self.service.view('AFKS', 'TQBR', 'M1')
        restored, _, _ = self.service.view('AFKS', 'TQBR', 'H1')
        self.assertIs(restored, first)
        self.assertEqual(restored.candles[-1].close, 8.513)

    def test_other_symbol_never_uses_existing_instrument_cache(self):
        self.seed()
        with patch('flowdesk.market_cache.load_moex_session_bundle', side_effect=ConnectionError('no data')):
            with self.assertRaises(ConnectionError):
                self.service.view('SBER', 'TQBR', 'H1')

    def test_session_policy_change_invalidates_stored_quotes(self):
        self.seed()
        with patch('flowdesk.market_cache.load_session_overrides', return_value={BASE.date(): None}), \
             patch('flowdesk.market_cache.load_moex_session_bundle', side_effect=ConnectionError('policy changed')):
            with self.assertRaises(ConnectionError):
                self.service.view('AFKS', 'TQBR', 'H1')

    def test_csv_higher_source_is_preserved_without_mutating_moex_cache(self):
        first, _, _ = self.seed()
        days = [Candle(BASE - timedelta(days=1), 8, 9, 7, 8)]
        _, parents, _ = self.service.view('AFKS', 'TQBR', 'H1', overrides={'D1': days},
                                         provenance={'D1': {'mode': 'USER_CSV'}})
        self.assertEqual(parents['D1'].candles, days)
        self.assertEqual(parents['D1'].session_info['mode'], 'USER_CSV')
        original, parents, _ = self.service.view('AFKS', 'TQBR', 'H1')
        self.assertIs(original, first)
        self.assertNotEqual(parents['D1'].candles, days)


if __name__ == '__main__':
    unittest.main()
