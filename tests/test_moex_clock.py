"""Exchange close confirmation is separate from the midnight chart label."""
from dataclasses import replace
from datetime import datetime,timedelta
import unittest

from flowdesk.market_data import Candle,MOSCOW,candle_close_time
from flowdesk.moex_clock import with_session_closes


def bar(day,hour=0,minute=0):
    return Candle(datetime.fromisoformat(day).replace(hour=hour,minute=minute,tzinfo=MOSCOW),100,110,90,105,123)


class TestMoexClock(unittest.TestCase):
    def test_final_hour_closes_at_2350_without_relabeling_or_changing_ohlcv(self):
        original=bar('2026-11-12',23)
        for board in ('TQBR','RFUD'):
            actual=with_session_closes([original],'H1',board)[0]
            self.assertEqual(candle_close_time(actual,'H1'),original.time.replace(minute=50))
            self.assertEqual(replace(actual,scheduled_close=None),original)

    def test_weekend_daily_and_weekly_close_at_1900(self):
        for tf,original in [('D1',bar('2026-11-08')),('W1',bar('2026-11-02'))]:
            actual=with_session_closes([original],tf,'TQBR')[0]
            self.assertEqual(actual.time,original.time)
            self.assertEqual(candle_close_time(actual,tf),bar('2026-11-08',19).time)

    def test_week_with_no_weekend_trading_closes_on_friday(self):
        actual=with_session_closes([bar('2026-10-19')],'W1','TQBR')[0]
        self.assertEqual(candle_close_time(actual,'W1'),bar('2026-10-23',23,50).time)

    def test_native_month_end_uses_last_physical_session_not_settlement_date(self):
        original=bar('2026-10-01')
        actual=with_session_closes([original],'MN1','TQBR')[0]
        self.assertEqual(candle_close_time(actual,'MN1'),bar('2026-10-31',19).time)
        self.assertEqual(actual.time,original.time)

    def test_closed_day_unknown_year_and_board_use_conservative_fallback(self):
        for original,board in [(bar('2026-01-01'),'TQBR'),(bar('2023-11-12'),'TQBR'),(bar('2026-11-12'),'OTHER')]:
            self.assertIs(with_session_closes([original],'D1',board)[0],original)

    def test_explicit_close_and_ordinary_hour_are_preserved(self):
        ordinary=bar('2026-11-12',12)
        explicit=replace(ordinary,scheduled_close=ordinary.time+timedelta(minutes=45))
        self.assertIs(with_session_closes([ordinary],'H1','TQBR')[0],ordinary)
        self.assertIs(with_session_closes([explicit],'H1','TQBR')[0],explicit)
        self.assertEqual(candle_close_time(explicit,'H1'),explicit.scheduled_close)

    def test_stream_final_hour_and_day_require_both_final_publications(self):
        from flowdesk.market_cache import MarketSnapshot
        from flowdesk.iss_plus import FeedEvent
        from flowdesk.stream_cache import merge_stream
        last=bar('2026-11-12',23)
        observed=last.time.replace(minute=49)
        saved=MarketSnapshot('SBER','TQBR',observed,[last],None,{'feed_delay_seconds':900},
                             {tf:[bar(day)] for tf,day in [('D1','2026-11-12'),('W1','2026-11-09'),('MN1','2026-11-01')]})
        received=bar('2026-11-13',0,6).time
        event=lambda tf,c:FeedEvent('candles','SBER','TQBR',1,received,tf,(c,))
        waiting,_=merge_stream(saved,[event('H1',last)],{})
        end=last.time.replace(minute=50)
        self.assertLess(waiting.asof,end)
        done,_=merge_stream(waiting,[event('D1',bar('2026-11-12'))],{})
        self.assertEqual(done.asof,end)

