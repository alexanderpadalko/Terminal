"""0.2.8: regular-session Pine candles use calendar bars, not settlement dates."""
import unittest
from datetime import date, datetime

from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW, aggregate, exclude_opening_auction


def bar(day, hh, op, hi, low, cl, volume=1000):
    return Candle(datetime(day.year, day.month, day.day, hh, tzinfo=MOSCOW),
                  op, hi, low, cl, volume)


class TestPhysicalCalendarChartBars(unittest.TestCase):
    def test_fvg_november_calendar_day_matches_pine_chart(self):
        # Same auction-cleaned H1 source; ONLY D1 grouping method differs.
        # A gap FVG 8.670..9.042 exists on Nov 7 (Sat). Rolling Nov 7/8 to
        # Nov 9 (settlement treatment) destroys it and invents 8.670..8.740.
        d5, d6, d7, d8, d9 = [date(2026, 11, n) for n in (5, 6, 7, 8, 9)]
        raw = [bar(d5, 7, 8.1, 8.670, 8.04, 8.593),
               bar(d6, 7, 8.630, 9.198, 8.610, 9.035),
               bar(d7, 9, 50, 50, 50, 50),  # auction only
               bar(d7, 10, 9.042, 9.180, 9.042, 9.150),
               bar(d8, 9, 50, 50, 50, 50),
               bar(d8, 10, 9.174, 9.306, 9.150, 9.243),
               bar(d9, 6, 50, 50, 50, 50),
               bar(d9, 7, 9.205, 9.208, 8.740, 8.813)]
        clean = exclude_opening_auction(raw, 'H1')
        self.assertEqual(len(clean), 5)
        physical = aggregate(clean, 'D1')
        self.assertEqual([c.time.date() for c in physical],
                         [d5, d6, d7, d8, d9])
        zones = analyze(physical, 'D1').fvg
        self.assertTrue(any(z.bottom == 8.670 and z.top == 9.042
                            and physical[z.created].time.date() == d7 for z in zones))
        settlement = aggregate(clean, 'D1', moex_trading_day=True)
        self.assertEqual([c.time.date() for c in settlement], [d5, d6, d9])
        self.assertTrue(any(z.bottom == 8.670 and z.top == 8.740
                            for z in analyze(settlement, 'D1').fvg))

    def test_extra_friday_stays_own_day_even_if_settled_monday(self):
        may1, may2, may3, may4 = [date(2026, 5, n) for n in (1, 2, 3, 4)]
        raw = [bar(may1, 9, 50, 50, 50, 50),
               bar(may1, 10, 10, 11, 9, 10),
               bar(may2, 10, 10, 12, 9, 11),
               bar(may3, 10, 11, 13, 10, 12),
               bar(may4, 7, 12, 14, 11, 13)]
        clean = exclude_opening_auction(raw, 'H1')
        self.assertEqual([c.time.date() for c in aggregate(clean, 'D1')],
                         [may1, may2, may3, may4])
        self.assertEqual([c.time.date() for c in aggregate(clean, 'W1')],
                         [date(2026, 4, 27), may4])
        self.assertEqual([c.time.date() for c in aggregate(clean, 'MN1')],
                         [may1])
        self.assertEqual(len(aggregate(clean, 'D1', moex_trading_day=True)), 1)

    def test_month_end_stays_correct_calendar_month(self):
        oct31, nov1, nov2 = date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)
        source = [bar(oct31, 10, 1, 2, 1, 2),
                  bar(nov1, 10, 2, 3, 2, 3),
                  bar(nov2, 7, 3, 4, 3, 4)]
        self.assertEqual(len(aggregate(source, 'D1')), 3)
        self.assertEqual(len(aggregate(source, 'W1')), 2)
        self.assertEqual([b.time.date() for b in aggregate(source, 'MN1')],
                         [date(2026, 10, 1), date(2026, 11, 1)])
        # Settlement metadata can still be requested without affecting charts.
        self.assertEqual(len(aggregate(source, 'D1', moex_trading_day=True)), 1)


if __name__ == '__main__':
    unittest.main()
