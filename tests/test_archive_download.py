"""Archive requests must not silently discard the liquidity initialization."""
import io
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from flowdesk.market_data import _iss_download, load_moex_session_bundle, Candle, MOSCOW
from flowdesk.moex_calendar import SessionOverride, REGULAR


def response(rows):
    return io.StringIO(json.dumps({'candles': {
        'columns': ['begin', 'open', 'high', 'low', 'close', 'volume'], 'data': rows}}))


ROWS = [['2026-11-10 10:00:00', 9, 10, 8, 9, 1],
        ['2026-11-10 11:00:00', 9, 11, 8, 9, 1]]


class TestArchiveDownload(unittest.TestCase):
    def test_observed_working_saturday_before_bundled_calendar_is_retained(self):
        day = datetime(2023, 11, 11, tzinfo=MOSCOW)
        bars = [Candle(day.replace(hour=9), 50, 50, 50, 50),
                Candle(day.replace(hour=10), 9, 10, 8, 9)]
        with patch('flowdesk.market_data._iss_download', return_value=bars):
            _, sources, info = load_moex_session_bundle('AFKS', 'H1', auction_overrides={})
        self.assertEqual(sources['H1'], bars[1:])
        self.assertEqual(info['observed_legacy_trading_days'], ['2023-11-11'])
        self.assertFalse(info['legacy_session_schedule_verified'])

    def test_explicit_old_session_override_is_not_replaced_by_inference(self):
        day = datetime(2023, 11, 11, tzinfo=MOSCOW)
        bars = [Candle(day.replace(hour=9), 9, 10, 8, 9)]
        with patch('flowdesk.market_data._iss_download', return_value=bars):
            _, sources, info = load_moex_session_bundle('AFKS', 'H1',
                                                       auction_overrides={day.date(): SessionOverride(None, REGULAR, day.date())})
        self.assertEqual(sources['H1'], bars)
        self.assertEqual(info['observed_legacy_trading_days'], [])

    def test_archive_capacity_overflow_never_returns_tail(self):
        with patch('flowdesk.market_data.urllib.request.urlopen', return_value=response(ROWS)):
            with self.assertRaisesRegex(ValueError, 'обрезанной'):
                _iss_download('AFKS', 60, days=100, limit=1, timeout=1,
                              board='TQBR', require_full=True)

    def test_temporary_reset_retries_same_page_without_duplicates(self):
        with patch('flowdesk.market_data.urllib.request.urlopen',
                   side_effect=[ConnectionResetError('temporary'), response(ROWS), response([])]) as request:
            with patch('flowdesk.market_data.time_module.sleep'):
                bars = _iss_download('AFKS', 60, days=100, limit=10, timeout=1,
                                     board='TQBR', require_full=True)
        self.assertEqual(len(bars), 2)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].args[0].full_url,
                         request.call_args_list[1].args[0].full_url)


if __name__ == '__main__':
    unittest.main()
