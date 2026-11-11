"""Archive requests must not silently discard the liquidity initialization."""
import io
import json
import unittest
from unittest.mock import patch

from flowdesk.market_data import _iss_download


def response(rows):
    return io.StringIO(json.dumps({'candles': {
        'columns': ['begin', 'open', 'high', 'low', 'close', 'volume'], 'data': rows}}))


ROWS = [['2026-11-10 10:00:00', 9, 10, 8, 9, 1],
        ['2026-11-10 11:00:00', 9, 11, 8, 9, 1]]


class TestArchiveDownload(unittest.TestCase):
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
