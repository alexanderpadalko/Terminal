import unittest
from datetime import datetime

from flowdesk.market_data import MOSCOW
from flowdesk.stream_probe_policy import minute_updates_expected


class TestProbeSession(unittest.TestCase):
    def test_midnight_preserves_fifteen_minute_delayed_tail(self):
        for board in ('TQBR', 'RFUD'):
            for moment, expected in [('2026-11-12T23:59:00', True),
                                     ('2026-11-13T00:04:00', True),
                                     ('2026-11-13T00:06:00', False),
                                     ('2026-11-13T00:16:00', False),
                                     ('2026-11-13T12:00:00', True)]:
                with self.subTest(board=board, moment=moment):
                    self.assertEqual(minute_updates_expected(datetime.fromisoformat(moment).replace(tzinfo=MOSCOW),board),expected)

    def test_unknown_schedule_does_not_excuse_missing_updates(self):
        self.assertTrue(minute_updates_expected(datetime(2027,1,1,tzinfo=MOSCOW),'RFUD'))
        self.assertTrue(minute_updates_expected(datetime(2026,11,13,tzinfo=MOSCOW),'UNKNOWN'))
