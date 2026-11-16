"""Open uses actual post-auction Finam prices, not a shifted daily Close."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import unittest

from flowdesk import moex_trading_family as moex
from flowdesk.market_data import Candle, MOSCOW

SYMBOL='CRZ6@RTSX'
NOW=datetime(2026,11,16,12,tzinfo=MOSCOW)


def bar(day,hour,o=10,hi=15,lo=5,c=11,v=100,minute=0):
    return Candle(datetime.fromisoformat(day).replace(hour=hour,minute=minute,tzinfo=MOSCOW),o,hi,lo,c,v)


def family(raw,coverage=None,observations=None,symbol=SYMBOL,now=NOW):
    known={(tf,b.time):now for tf,rows in raw.items() for b in rows}
    return moex.build_family(raw,symbol,now,observations=known if observations is None else observations,
                             archive_verified=True,coverage=coverage)


class TestContinuousOpen(unittest.TestCase):
    def test_cr_week_19_oct_uses_saturday_continuous_open_and_finam_last_close(self):
        # Finam HTTP boundary probe: 09:59 auction 12.642, 10:00 O 12.648.
        values=[('17',12.642,12.652,12.640,12.649,61731),
                ('18',12.649,12.650,12.632,12.638,80511),
                ('19',12.644,12.664,12.533,12.578,9841114),
                ('20',12.575,12.605,12.522,12.531,6943947),
                ('21',12.537,12.553,12.400,12.404,6387172),
                ('22',12.406,12.412,12.291,12.326,8242763),
                ('23',12.323,12.323,12.181,12.227,8100583)]
        daily=[bar('2026-10-'+d,7,o,h,l,c,v) for d,o,h,l,c,v in values]
        # Only the first physical day of each trading day determines Open.
        lower=[bar('2026-10-17',9,12.642,12.642,12.642,12.642,1443,minute=59),
               bar('2026-10-17',10,12.648,12.648,12.642,12.645,679)]
        lower.extend(bar('2026-10-'+d,7,o,h,l,c,v) for d,o,h,l,c,v in values if int(d)>=20)
        raw={'D1':daily,'M1':lower}
        result,status,detail=family(raw)
        self.assertEqual(status,'ready')
        weekly=result['W1'][0]
        self.assertEqual((weekly.open,weekly.high,weekly.low,weekly.close),(12.648,12.664,12.181,12.227))
        self.assertEqual(weekly.volume,sum(row.volume for row in daily))
        self.assertEqual(result['D1'][0].open,12.648)
        self.assertEqual(result['MN1'][0].open,12.648)
        self.assertEqual(weekly.trading_date,date(2026,10,19))
        self.assertEqual(daily[0].open,12.642)
        self.assertEqual(detail['continuous_open_days'],5)

    def test_all_ten_futures_and_next_series_use_the_same_calendar_rule(self):
        for symbol in ('SiZ6@RTSX','CRZ6@RTSX','EUZ6@RTSX','EDZ6@RTSX','MXZ6@RTSX',
                       'RIZ6@RTSX','USDRUB.P@RTSX','CNYRUB.P@RTSX','EURRUB.P@RTSX','IRUS.P@RTSX',
                       'CRH7@RTSX'):
            with self.subTest(symbol=symbol):
                raw={'D1':[bar('2026-10-17',7)],'H1':[bar('2026-10-17',9,o=9),bar('2026-10-17',10,o=12)]}
                result,status,_=family(raw,symbol=symbol)
                self.assertEqual(status,'ready')
                self.assertTrue(all(result[tf][0].open==12 for tf in ('D1','W1','MN1')))

    def test_weekday_start_changes_from_nine_to_seven_and_extra_holiday_starts_at_ten(self):
        for day,hour in (('2026-04-01',9),('2026-10-01',7),('2026-11-04',10)):
            with self.subTest(day=day):
                raw={'D1':[bar(day,7)],'H1':[bar(day,hour-1,o=9),bar(day,hour,o=12)]}
                result,status,_=family(raw)
                self.assertEqual(status,'ready')
                self.assertEqual(result['D1'][0].open,12)

    def test_missing_open_blocks_family_instead_of_retaining_auction_open(self):
        for lower in ([],[bar('2026-10-17',9)], [bar('2026-10-17',11,o=12,v=99)]):
            result,status,_=family({'D1':[bar('2026-10-17',7)],'H1':lower})
            self.assertIsNone(result)
            self.assertEqual(status,'opening_source_unavailable')

    def test_illiquid_later_first_trade_needs_certified_leading_coverage(self):
        raw={'D1':[bar('2026-10-17',7)],'H1':[bar('2026-10-17',11,o=12,v=99)]}
        coverage={'H1':[(datetime(2026,10,16,7,tzinfo=MOSCOW),datetime(2026,10,17,12,tzinfo=MOSCOW))]}
        result,status,_=family(raw,coverage)
        self.assertEqual(status,'ready');self.assertEqual(result['D1'][0].open,12)
        self.assertEqual(family(raw,{'H1':[(raw['H1'][0].time,coverage['H1'][0][1])]})[1],
                         'opening_source_unavailable')

    def test_sparse_day_can_prove_first_trade_by_complete_native_volume_and_hlc(self):
        native=bar('2026-04-20',7,o=1.17,hi=1.17,lo=1.1696,c=1.1696,v=7)
        first=bar('2026-04-20',18,o=1.17,hi=1.17,lo=1.17,c=1.17,v=6)
        last=bar('2026-04-20',19,o=1.1696,hi=1.1696,lo=1.1696,c=1.1696,v=1)
        raw={'D1':[native],'H1':[first,last]}
        self.assertEqual(family(raw)[0]['D1'][0].open,1.17)
        # Missing one contract, different extreme, or different final price
        # cannot certify the supposedly empty prefix.
        for source in (replace(last,volume=0),replace(last,high=1.18),replace(last,close=1.17,high=1.17)):
            with self.subTest(source=source):
                self.assertEqual(family({'D1':[native],'H1':[first,source]})[1],'opening_source_unavailable')

    def test_finer_exact_source_wins_and_first_minute_close_is_never_used_as_open(self):
        raw={'D1':[bar('2026-10-17',7)],'M1':[bar('2026-10-17',10,o=12,c=13)],
             'H1':[bar('2026-10-17',10,o=11)]}
        self.assertEqual(family(raw)[0]['D1'][0].open,12)
        bad={'D1':raw['D1'],'M1':[bar('2026-10-17',10,o=16,hi=17)]}
        self.assertEqual(family(bad)[1],'opening_source_conflict')

    def test_future_snapshot_and_unobserved_live_source_are_not_used(self):
        raw={'D1':[bar('2026-11-16',7)],'H1':[bar('2026-11-16',7,o=12)]}
        known={('D1',raw['D1'][0].time):NOW,('H1',raw['H1'][0].time):NOW+timedelta(days=1)}
        self.assertEqual(family(raw,observations=known)[1],'opening_source_unavailable')
        now=NOW.replace(hour=7,minute=30)
        known={('D1',raw['D1'][0].time):now}
        self.assertEqual(family(raw,observations=known,now=now)[1],'opening_source_unavailable')
        known['H1',raw['H1'][0].time]=now
        self.assertEqual(family(raw,observations=known,now=now)[0]['D1'][0].open,12)

    def test_unverified_old_hours_preserve_native_history(self):
        for day in ('2025-08-22','2026-03-20'):
            result,status,detail=family({'D1':[bar(day,8)]},symbol='MXZ6@RTSX')
            self.assertEqual(status,'ready');self.assertEqual(result['D1'][0].open,10)
            self.assertEqual(detail['native_open_history_days'],1)
