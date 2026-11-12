"""Contract tests: trace H1 flow origins without modifying levels."""
import unittest
from datetime import datetime, timedelta
from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW

BASE = datetime(2026, 11, 6, 7, tzinfo=MOSCOW)

def bar(i, high, low, close):
    return Candle(BASE + timedelta(hours=i), close, high, low, close, 100)

class TestRangeProvenance(unittest.TestCase):
    def setUp(self):
        self.h1 = analyze([bar(0,10,8,9),bar(1,11,9,10.8),
                           bar(2,10,7.5,7.8),bar(3,9.5,8,8.5)], "H1")

    def test_rule2_records_real_origin(self):
        f = self.h1.flows[2]
        self.assertEqual((f.rule,f.side), (2,-1))
        self.assertEqual(f.last_change_rule,2)
        self.assertEqual(f.last_change_at, BASE + timedelta(hours=2))
        self.assertIn("встречного fvg нет", f.range_origin.lower())
        self.assertEqual(f.range_high_at,BASE + timedelta(hours=1))
        self.assertEqual(f.lo,7.5)

    def test_provenance_survives_h1_to_m5_projection(self):
        lower = [Candle(BASE + timedelta(minutes=5*i),9,10,8,9,100) for i in range(48)]
        m5 = analyze(lower,"M5",higher_h1=self.h1)
        confirmed,original = m5.flows[35],self.h1.flows[2]
        for name in ("last_change_at","range_origin","range_low_at","range_high_at"):
            self.assertEqual(getattr(confirmed,name),getattr(original,name))
        self.assertEqual((confirmed.lo,confirmed.hi),(original.lo,original.hi))
        self.assertEqual(confirmed.source_confirmed_at,BASE + timedelta(hours=3))

    def test_provenance_not_in_future(self):
        for f in self.h1.flows:
            if not f.side: continue
            for name in ("range_start_at","last_change_at","range_low_at","range_high_at"):
                self.assertIsNotNone(getattr(f,name))
            self.assertLessEqual(f.range_low_at,self.h1.candles[f.i].time)
            self.assertLessEqual(f.range_high_at,self.h1.candles[f.i].time)

