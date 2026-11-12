from datetime import datetime, date, time, timedelta
import io
import json
import unittest
from unittest.mock import patch

from flowdesk.futures_data import iss_symbol, clean, aggregate_h4, auction
from flowdesk.market_data import Candle, MOSCOW, _iss_download, exclude_opening_auction


class TestFuturesData(unittest.TestCase):
    def test_native_case_and_correct_futures_endpoint(self):
        self.assertEqual(iss_symbol('SIZ6'), 'SiZ6')
        self.assertEqual(iss_symbol('EUZ6'), 'EuZ6')
        self.assertEqual(iss_symbol('USDRUBF'), 'USDRUBF')
        one = io.BytesIO(json.dumps({'candles': {'columns': ['begin', 'open', 'high', 'low', 'close', 'volume'],
            'data': [['2026-11-12 07:00:00', 10, 11, 9, 10, 100]]}}).encode())
        empty = io.BytesIO(json.dumps({'candles': {'columns': [], 'data': []}}).encode())
        with patch('flowdesk.market_data.urllib.request.urlopen', side_effect=[one, empty]) as http:
            _iss_download('SIZ6', 60, days=3, limit=1000, timeout=15, board='RFUD')
        self.assertIn('/engines/futures/markets/forts/boards/RFUD/securities/SiZ6/', http.call_args.args[0].full_url)

    def test_rfud_auction_is_independent_of_tqbr(self):
        self.assertEqual(auction(date(2026, 6, 1)), (time(8, 50), time(9)))
        self.assertEqual(auction(date(2026, 11, 12)), (time(6, 50), time(7)))
        start = datetime(2026, 6, 1, 8, 50, tzinfo=MOSCOW)
        candles = [Candle(start, 10, 11, 9, 10), Candle(start.replace(hour=9, minute=0), 10, 11, 9, 10)]
        self.assertEqual(exclude_opening_auction(candles, 'M1', board='RFUD'), candles[1:])

    def test_h4_preserves_ohlcv_and_physical_moscow_session(self):
        start = datetime(2026, 11, 12, 7, tzinfo=MOSCOW)
        bars = [Candle(start + timedelta(hours=i), 10, 12 + i, 9, 11, i + 1) for i in range(17)]
        result = aggregate_h4(bars)
        self.assertEqual([c.time.hour for c in result], [7, 11, 15, 19, 23])
        self.assertEqual(sum(c.volume for c in result), sum(c.volume for c in bars))
        self.assertEqual(result[-1].scheduled_close, start.replace(hour=0) + timedelta(days=1))
