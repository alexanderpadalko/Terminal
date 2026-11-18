import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowdesk.engine import FVGEngine, StructureEngine, PairEngine, analyze
from flowdesk.market_data import Candle, aggregate, parse_time, read_csv, without_session_zeros, MOSCOW

BASE = datetime(2026, 6, 1, 10, 0, tzinfo=MOSCOW)


def bar(i, o, h, l, c):
    return Candle(BASE + timedelta(hours=i), o, h, l, c, 100)


class TestCandles(unittest.TestCase):
    def test_invalid_ohlc_rejected(self):
        with self.assertRaises(ValueError):
            bar(0, 9, 10, 8, 12)

    def test_time_parsing_timezone(self):
        self.assertEqual(parse_time("2026-06-01T10:00:00+03:00").utcoffset(), timedelta(hours=3))
        self.assertEqual(parse_time("1780297200").tzinfo, timezone.utc)
        self.assertEqual(parse_time("2026-06-01 10:00:00").utcoffset(), timedelta(hours=3))

    def test_csv_import_standard_and_reordered(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tv.csv"
            path.write_text("time,close,low,high,open,volume\n2026-06-01T10:00:00+03:00,9,8,10,9.5,100\n2026-06-01T11:00:00+03:00,10,9,11,9.5,101\n", encoding="utf8")
            rows = read_csv(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1].close, 10)
            self.assertEqual(rows[1].volume, 101)

    def test_csv_dedupe_and_sort(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tv.csv"
            path.write_text("Time,Open,High,Low,Close\n2026-06-01 11:00:00,9,10,8,9\n2026-06-01 10:00:00,9,10,8,9\n2026-06-01 11:00:00,9,11,8,9\n")
            rows = read_csv(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1].high, 11)

    def test_m5_grouping(self):
        bars = [Candle(BASE + timedelta(minutes=i), 10 + i, 11 + i, 9 + i, 10.5 + i, 20) for i in range(7)]
        res = aggregate(bars, "M5")
        self.assertEqual(len(res), 2)
        self.assertEqual((res[0].open, res[0].high, res[0].low, res[0].close, res[0].volume), (10, 15, 9, 14.5, 100))
        self.assertEqual(res[1].time.minute, 5)

    def test_week_resampling_mon_start(self):
        b = [Candle(BASE + timedelta(days=i), 10, 11, 9, 10, 1) for i in range(9)]
        res = aggregate(b, "W1")
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].volume, 7)

    def test_session_zero_filter_optional(self):
        a = bar(0, 9, 10, 8, 9)
        b = Candle(BASE + timedelta(days=1), 10, 10, 10, 10)
        self.assertEqual(len(without_session_zeros([a, b])), 1)


class TestFvg(unittest.TestCase):
    def test_bull_requires_no_adjacent_gap(self):
        vals = [bar(0, 9, 10, 8, 9), bar(1, 10, 11, 9.5, 10), bar(2, 11, 12, 10.5, 11)]
        engine = FVGEngine("H1")
        for c in vals:
            zone = engine.step(c)
        self.assertIsNotNone(zone)
        self.assertEqual((zone.bottom, zone.top, zone.side), (10, 10.5, 1))
        self.assertTrue(zone.active)
        engine.step(bar(3, 11, 12, 9.5, 9.8))
        self.assertFalse(zone.active)
        self.assertTrue(zone.balanced)
        self.assertEqual(zone.broken, 3)

    def test_gap_filter(self):
        bars = [bar(0, 9, 10, 8, 9), bar(1, 11, 12, 10.2, 11), bar(2, 11.5, 12, 11, 11.7)]
        eng = FVGEngine("H1", exclude_gaps=True)
        for b in bars:
            eng.step(b)
        self.assertEqual(eng.zones, [])

    def test_bear_zone(self):
        bars = [bar(0, 11, 12, 10, 11), bar(1, 10.5, 11, 9, 9.9), bar(2, 8.6, 9.5, 8, 8.9)]
        eng = FVGEngine("D1")
        for b in bars:
            eng.step(b)
        self.assertEqual(len(eng.zones), 1)
        self.assertEqual((eng.zones[0].top, eng.zones[0].bottom), (10, 9.5))

    def test_strict_non_inside_m1_h1(self):
        a = bar(0, 9, 10, 8, 9)
        b = bar(1, 9.5, 9.9, 8.1, 9.6)
        c = bar(2, 11, 12, 10.4, 11)
        engine = FVGEngine("H1")
        for v in (a, b, c):
            engine.step(v)
        self.assertEqual(engine.zones, [])

    def test_m5_checkpoint_keeps_absolute_zone_indices_and_isolates_forming_break(self):
        def m5(i, o, h, l, c):
            return Candle(BASE + timedelta(minutes=5 * i), o, h, l, c, 100)

        prefix = [m5(i, 9, 10, 8, 9) for i in range(512)]
        prefix += [m5(512, 10, 11, 9, 10), m5(513, 11, 12, 10, 11),
                   m5(514, 12, 13, 11.5, 12)]
        forming = m5(515, 11, 12, 10.5, 10.8)
        options = dict(asof=forming.time + timedelta(minutes=1),
                       market_structure=False, low_tf_flow=False, incremental=True)
        before = analyze([*prefix, forming], 'M5', **options)
        zone = before.confirmed_fvg[-1]
        self.assertEqual((zone.start, zone.impulse, zone.created), (512, 513, 514))
        self.assertTrue(zone.active)
        self.assertEqual(before.fvg[-1].broken, 515)
        fvg = before.replay_checkpoint.engines[1]
        self.assertEqual(list(fvg._bars), prefix[-2:])

        changed = [*prefix, m5(515, 11, 12, 10.5, 11.2)]
        resumed = analyze(changed, 'M5', cached=before, **options)
        fresh = analyze(changed, 'M5', **options)
        self.assertEqual(resumed.fvg, fresh.fvg)
        self.assertEqual(resumed.confirmed_fvg, fresh.confirmed_fvg)
        self.assertTrue(resumed.fvg[-1].balanced)
        self.assertTrue(resumed.fvg[-1].active)
        self.assertEqual(before.fvg[-1].broken, 515)


class TestStructure(unittest.TestCase):
    def test_mother_inside_and_reversal(self):
        eng = StructureEngine()
        eng.step(bar(0, 9, 10, 8, 9), 0)
        s = eng.step(bar(1, 10, 11, 9, 10), 1)
        self.assertEqual(s.direction, 1)
        eng.step(bar(2, 10, 10.5, 9.5, 10), 2)
        self.assertEqual(eng.mother_lo, 9)
        s = eng.step(bar(3, 9.1, 10, 8.5, 8.8), 3)
        self.assertEqual(s.direction, -1)
        self.assertTrue(s.turn_down)
        self.assertEqual(len(eng.segments), 1)
        self.assertEqual((eng.segments[0].pa, eng.segments[0].pb), (8, 11))

    def test_outside_does_not_create_intermediate_pivots(self):
        eng = StructureEngine()
        eng.step(bar(0, 9, 10, 8, 9), 0)
        eng.step(bar(1, 9, 11, 8.5, 10), 1)
        eng.step(bar(2, 10, 12, 8, 11), 2)  # both mother bounds broken
        self.assertEqual(eng.outside_origin, 1)
        self.assertEqual(len(eng.segments), 0)
        eng.step(bar(3, 10, 13, 7, 11), 3)  # continuation, not second pivot
        self.assertEqual(len(eng.segments), 0)
        self.assertEqual(eng.outside_base_segment.b, 3)
        eng.step(bar(4, 11, 13.5, 7.5, 12), 4)  # one-sided high breakout
        self.assertEqual(eng.outside_origin, 0)
        self.assertGreaterEqual(len(eng.segments), 1)

    def test_flow_rule_2_closing_boundary(self):
        bars = [bar(0, 9.5, 10, 9, 9.5), bar(1, 9.8, 11, 9.3, 10.8), bar(2, 9.5, 10, 8.6, 8.8)]
        a = analyze(bars, "H1")
        self.assertEqual(a.flows[1].side, 1)
        self.assertEqual(a.flows[-1].rule, 2)
        self.assertEqual(a.flows[-1].side, -1)


if __name__ == "__main__":
    unittest.main()

class TestTimeframes(unittest.TestCase):
    def test_infer_h1(self):
        from flowdesk.market_data import infer_timeframe
        data = [bar(i, 9, 10, 8, 9) for i in range(8)]
        self.assertEqual(infer_timeframe(data), "H1")
