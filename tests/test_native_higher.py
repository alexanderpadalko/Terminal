"""Native D/W/M OHLCV, independent history and coherent cache migration."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flowdesk.market_cache import MarketService, MarketSnapshot
from flowdesk.market_data import (Candle, MOSCOW, NATIVE_HIGHER, CANONICAL_H1_DAYS,
                                  aggregate, download_native_higher, download_moex,
                                  load_moex_session_bundle)


BASE = datetime(2026, 11, 11, tzinfo=MOSCOW)
HOURS = [Candle(BASE.replace(hour=h), 10, 12, 9, 11, 100) for h in (7, 8, 9)]


def native_data(extra_day=False, revised=False):
    # Deliberately different prices and longer history than the H1 fixture.
    old = Candle(BASE.replace(year=2010, month=1, day=1), 20, 30, 10, 25, 1000)
    day = Candle(BASE, 50, 60 if revised else 55, 9, 11, 999)
    result = {tf: [Candle(aggregate([old], tf)[0].time, old.open, old.high, old.low, old.close, old.volume),
                   Candle(aggregate([day], tf)[0].time, day.open, day.high, day.low, day.close, day.volume)]
              for tf in NATIVE_HIGHER}
    if extra_day:
        result['D1'].insert(1, Candle(BASE - timedelta(days=1), 30, 40, 20, 35, 1500))
    return result


def bundle(native=None, hours=HOURS):
    native = native if native is not None else native_data()
    return hours, {'H1': hours, **native}, {'higher_from_clean_h1': False,
                                          'raw_h1_bars': len(hours), 'auction_h1_removed': 0}


class TestNativeHigherDownload(unittest.TestCase):
    def test_native_intervals_preserve_auction_prices_and_volume(self):
        raw = [Candle(BASE.replace(hour=6, minute=50), 50, 60, 8, 10, 999)]
        for tf, interval in (('D1', 24), ('W1', 7), ('MN1', 31)):
            with self.subTest(tf=tf), patch('flowdesk.market_data._iss_download', return_value=raw) as request:
                result = download_native_higher('afks', tf, days=77, board='tqbr')
                self.assertEqual(request.call_args.args, ('AFKS', interval))
                self.assertEqual(request.call_args.kwargs['days'], 77)
                self.assertTrue(request.call_args.kwargs['require_full'])
                self.assertEqual([(b.open, b.high, b.low, b.close, b.volume) for b in result], [(50, 60, 8, 10, 999)])
                self.assertEqual(result[0].time.hour, 0)
        self.assertEqual(result[0].time, BASE.replace(day=1))

    def test_native_download_does_not_consult_intraday_auction_calendar(self):
        raw = [Candle(BASE, 50, 60, 8, 10, 999)]
        with patch('flowdesk.market_data._iss_download', return_value=raw), \
             patch('flowdesk.market_data.load_session_overrides', side_effect=AssertionError('not needed')):
            result = download_moex('AFKS', 'D1')
        self.assertEqual(result, raw)

    def test_bundle_uses_independent_full_native_history(self):
        def exchange(symbol, interval, **kwargs):
            if interval == 60:
                return HOURS
            tf = {24: 'D1', 7: 'W1', 31: 'MN1'}[interval]
            return native_data()[tf]
        with patch('flowdesk.market_data._iss_download', side_effect=exchange) as request:
            _, series, meta = load_moex_session_bundle('AFKS', 'D1', history_days=2,
                                                       higher_history_days={'D1': 42})
        calls = {c.args[1]: c.kwargs for c in request.call_args_list}
        self.assertEqual(calls[60]['days'], 2)
        self.assertEqual(calls[24]['days'], 42)
        self.assertEqual(calls[7]['days'], CANONICAL_H1_DAYS)
        self.assertEqual(series['D1'], native_data()['D1'])
        self.assertLess(series['D1'][0].time, series['H1'][0].time)
        self.assertEqual(series['D1'][-1].high, 55)
        self.assertEqual(max(b.high for b in series['H1']), 12)
        self.assertTrue(meta['sources']['D1']['native_higher'])
        self.assertFalse(meta['sources']['D1']['opening_auction_excluded'])
        self.assertTrue(meta['sources']['H1']['opening_auction_excluded'])


class TestNativeHigherCache(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'cache.sqlite3'
        self.now = BASE.replace(hour=11, minute=16)
        self.service = MarketService(self.path, clock=lambda: self.now)
        calendar = patch('flowdesk.market_cache.load_session_overrides', return_value={})
        calendar.start()
        self.addCleanup(calendar.stop)

    def seed(self):
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle()):
            return self.service.view('AFKS', 'TQBR', 'H1')

    def test_native_ohlcv_and_provenance_survive_restart_without_network(self):
        self.seed()
        self.now += timedelta(days=40)
        restarted = MarketService(self.path, clock=lambda: self.now)
        with patch('flowdesk.market_cache.load_moex_session_bundle', side_effect=AssertionError('network')) as request:
            for tf in NATIVE_HIGHER:
                result, _, info = restarted.view('AFKS', 'TQBR', tf)
                self.assertEqual(result.candles, native_data()[tf])
                self.assertTrue(result.session_info['native_higher'])
                self.assertFalse(result.session_info['opening_auction_excluded'])
                self.assertFalse(result.session_info['higher_from_clean_h1'])
                self.assertEqual(info['asof'], BASE.replace(hour=11, minute=16).isoformat())
                self.assertEqual(result.confirmed_bars, 1)
        request.assert_not_called()

    def test_migration_downloads_native_archive_and_only_recent_h1(self):
        key = self.service.key('AFKS', 'TQBR', {})
        old_hour = Candle(BASE.replace(year=2014, month=6, day=9, hour=10), 10, 12, 9, 11)
        self.service.store.write(key, MarketSnapshot('AFKS', 'TQBR', self.now,
                                                     [old_hour, *HOURS], None, {'old_version': '0.2.12'}))
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle()) as request:
            result, parents, _ = self.service.view('AFKS', 'TQBR', 'H1')
        self.assertEqual(request.call_args.kwargs['history_days'], 2)
        self.assertEqual(request.call_args.kwargs['higher_history_days'], {tf: CANONICAL_H1_DAYS for tf in NATIVE_HIGHER})
        self.assertEqual(result.candles, [old_hour, *HOURS])
        self.assertEqual(parents['D1'].candles, native_data()['D1'])
        self.assertEqual(self.service.store.read(key).native_higher, native_data())

    def test_failed_migration_keeps_old_cache_and_never_substitutes_derived_higher(self):
        key = self.service.key('AFKS', 'TQBR', {})
        self.service.store.write(key, MarketSnapshot('AFKS', 'TQBR', self.now, HOURS, None, {}))
        with patch('flowdesk.market_cache.load_moex_session_bundle', side_effect=ConnectionError('native unavailable')):
            with self.assertRaisesRegex(ConnectionError, 'native unavailable'):
                self.service.view('AFKS', 'TQBR', 'D1')
        restored = self.service.store.read(key)
        self.assertEqual(restored.hours, HOURS)
        self.assertEqual(restored.native_higher, {})

    def test_refresh_overwrites_forming_native_bar_and_keeps_early_history(self):
        self.seed()
        self.now = BASE.replace(hour=12)
        updated = {tf: bars[-1:] for tf, bars in native_data(revised=True).items()}
        with patch('flowdesk.market_cache.load_moex_session_bundle', return_value=bundle(updated, HOURS[-1:])) as request:
            daily, _, _ = self.service.view('AFKS', 'TQBR', 'D1', refresh=True)
        self.assertEqual(len(daily.candles), 2)
        self.assertEqual(daily.candles[0], native_data()['D1'][0])
        self.assertEqual(daily.candles[-1].high, 60)
        self.assertEqual(daily.confirmed_bars, 1)
        self.assertEqual(request.call_args.kwargs['higher_history_days'], {'D1': 2, 'W1': 4, 'MN1': 12})

    def test_failed_refresh_retains_one_coherent_old_snapshot(self):
        main, parents, _ = self.seed()
        self.now += timedelta(days=1)
        calls = []
        def exchange(symbol, interval, **kwargs):
            calls.append(interval)
            if interval == 31:
                raise ConnectionError('monthly failed')
            return HOURS if interval == 60 else native_data(revised=True)[{24: 'D1', 7: 'W1'}[interval]]
        with patch('flowdesk.market_data._iss_download', side_effect=exchange):
            result, higher, info = self.service.view('AFKS', 'TQBR', 'H1', refresh=True)
        self.assertEqual(calls, [60, 24, 7, 31])
        self.assertIs(result, main)
        self.assertIs(higher['MN1'], parents['MN1'])
        self.assertEqual(higher['D1'].candles[-1].high, 55)
        self.assertEqual(info['tier'], 'offline')
        self.assertEqual(result.asof, BASE.replace(hour=11, minute=16))

    def test_native_csv_override_is_separate_from_persisted_exchange_data(self):
        self.seed()
        csv = [Candle(BASE - timedelta(days=1), 70, 80, 60, 75)]
        _, parents, _ = self.service.view('AFKS', 'TQBR', 'H1', overrides={'D1': csv},
                                         provenance={'D1': {'mode': 'USER_CSV'}})
        self.assertEqual(parents['D1'].candles, csv)
        result, _, _ = self.service.view('AFKS', 'TQBR', 'D1')
        self.assertEqual(result.candles, native_data()['D1'])


if __name__ == '__main__':
    unittest.main()
