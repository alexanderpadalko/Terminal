"""Shared Last-price OHLCV for all eight products without venue/price mixing."""
from contextlib import closing
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json,tempfile,unittest

from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle,serialize_candle
from flowdesk.provider_candles import NATIVE_HIGHER
from flowdesk.session_store import record_response
from flowdesk.trading_calendar import calendar_for,CalendarUnavailable
from flowdesk.us_futures_family import (POLICY,product,enabled,calendar_for_family,
                                       build_family,backfill)

UTC=timezone.utc
SYMBOLS=('NQZ26@XCME','ESZ26@XCME','GCZ26@XCEC','SIZ26@XCEC',
         'PLF27@XNYM','PAZ26@XNYM','NGZ26@XNYM','BRNF27@IFEU')
NOW=datetime(2026,11,14,12,tzinfo=UTC)


def source(symbol,days=(date(2026,11,12),date(2026,11,13)),base=100):
    cal=calendar_for_family(calendar_for(symbol));hours=[];daily=[]
    for day in days:
        session=cal.daily_session(day)
        for a,b in session.segments:
            while a<b:
                value=base+len(hours)%9
                hours.append(Candle(a,value,value+3,value-2,value+1,10+len(hours),
                                    scheduled_close=a+timedelta(hours=1)))
                a=min(a+timedelta(hours=1),b)
        stamp=datetime.combine(day,datetime.min.time(),UTC)+timedelta(hours=5)
        daily.append(Candle(stamp,base,base+30,base-20,base-10,999))
    observed=max(b.scheduled_close for b in hours)+timedelta(days=1)
    raw={'H1':hours,'D1':daily}
    stamps={('H1',b.time):observed for b in hours}
    coverage={'H1':[(hours[0].time,observed)]}
    return raw,cal,observed,stamps,coverage


class TestUSFuturesFamily(unittest.TestCase):
    def test_all_eight_products_and_following_series_are_enabled(self):
        for symbol in (*SYMBOLS,'NQH27@XCME','GCJ2027@XCEC','BRNG2027@IFEU'):
            with self.subTest(symbol=symbol):
                self.assertTrue(product(symbol));self.assertTrue(enabled(symbol,calendar_for(symbol)))

    def test_venue_and_root_are_both_required_including_russian_silver_name_collision(self):
        for symbol in ('SIZ26@RTSX','NGZ26@RTSX','NQZ26@MISX','SBER@MISX','BTCUSDT@BINANCE','CLZ26@XNYM'):
            self.assertIsNone(product(symbol))
        # A Brent alias on another MIC is not proof of the IFEU schedule.
        self.assertFalse(enabled('BRNF27@XNYM',calendar_for('BRNF27@IFEU')))

    def test_all_ohlcv_and_dwm_use_the_same_hourly_source_not_native_prices(self):
        for symbol in SYMBOLS:
            with self.subTest(symbol=symbol):
                raw,cal,now,obs,coverage=source(symbol)
                family,status,_=build_family(raw,cal,now,coverage=coverage,observations=obs)
                self.assertEqual(status,'ready');self.assertEqual(len(family['D1']),2)
                for tf,bars in family.items():
                    for bar in bars:
                        hours=[h for h in raw['H1'] if bar.time<=h.time<bar.scheduled_close]
                        self.assertEqual((bar.open,bar.high,bar.low,bar.close,bar.volume),
                            (hours[0].open,max(h.high for h in hours),min(h.low for h in hours),hours[-1].close,sum(h.volume for h in hours)))
                        self.assertNotEqual(bar.close,raw['D1'][-1].close)
                        self.assertFalse(bar.settlement_close)

    def test_cme_equities_metals_energy_have_distinct_labor_day_closes(self):
        for symbol in SYMBOLS[:-1]:
            cal=calendar_for_family(calendar_for(symbol));session=cal.session(date(2026,9,7))
            self.assertEqual(session.end.astimezone(cal.zone).time(),
                             datetime.min.time().replace(hour=12) if cal.profile=='CME_EQUITY' else datetime.min.time().replace(hour=13,minute=30))
            self.assertIsNone(cal.daily_session(date(2026,9,7)))
            self.assertEqual(len(cal.daily_session(date(2026,9,8)).segments),2)

    def test_metals_and_gas_keep_the_final_half_hour_of_the_holiday(self):
        for symbol in ('GCZ26@XCEC','SIZ26@XCEC','PLF27@XNYM','PAZ26@XNYM','NGZ26@XNYM'):
            raw,cal,now,obs,coverage=source(symbol,days=(date(2026,9,8),))
            family,status,_=build_family(raw,cal,now,coverage=coverage,observations=obs)
            self.assertEqual(status,'ready')
            self.assertEqual(family['D1'][0].volume,sum(h.volume for h in raw['H1']))
            holiday=cal.session(date(2026,9,7))
            last=[b for b in raw['H1'] if holiday.start<=b.time<holiday.end][-1]
            self.assertEqual(last.time.astimezone(cal.zone).hour,13)
            self.assertGreater(last.scheduled_close,holiday.end)

    def test_july_holiday_stops_at_noon_for_cme_products(self):
        for symbol in SYMBOLS[:-1]:
            cal=calendar_for_family(calendar_for(symbol))
            self.assertEqual(cal.session(date(2026,7,3)).end.astimezone(cal.zone).hour,12)
            self.assertEqual(len(cal.daily_session(date(2026,7,6)).segments),2)

    def test_brent_uses_london_dst_and_sunday_open_not_chicago(self):
        cal=calendar_for_family(calendar_for('BRNF27@IFEU'))
        self.assertEqual(cal.session(date(2026,10,27)).start.hour,0)
        self.assertEqual(cal.session(date(2026,10,27)).end.hour,22)
        self.assertEqual(cal.session(date(2026,11,3)).start.hour,1)
        self.assertEqual(cal.session(date(2026,11,3)).end.hour,23)
        self.assertEqual(cal.session(date(2026,11,2)).start,datetime(2026,11,1,23,tzinfo=UTC))
        self.assertEqual(cal.session(date(2026,11,26)).end.hour,18)
        self.assertEqual(cal.session(date(2026,11,26)).end.minute,30)

    def test_unverified_cme_holiday_is_not_replaced_by_equity_hours(self):
        cal=calendar_for_family(calendar_for('GCZ26@XCEC'))
        with self.assertRaises(CalendarUnavailable):cal.session(date(2026,12,24))

    def test_verified_november_27_product_closes_are_distinct(self):
        for symbol in SYMBOLS[:-1]:
            with self.subTest(symbol=symbol):
                cal=calendar_for_family(calendar_for(symbol))
                session=cal.daily_session(date(2026,11,27))
                expected=(12,15) if symbol.startswith(('NQ','ES')) else (13,45)
                closing=session.end.astimezone(cal.zone)
                self.assertEqual((closing.hour,closing.minute),expected)
                self.assertEqual(len(session.segments),2)
                self.assertEqual(session.start,datetime(2026,11,25,23,tzinfo=UTC))
                self.assertEqual(session.segments[0][1],datetime(2026,11,26,
                    18 if symbol.startswith(('NQ','ES')) else 19,
                    0 if symbol.startswith(('NQ','ES')) else 30,tzinfo=UTC))

    def test_november_27_last_partial_hour_is_kept_without_after_close_prices(self):
        for symbol in SYMBOLS[:-1]:
            with self.subTest(symbol=symbol):
                raw,cal,now,obs,coverage=source(symbol,days=(date(2026,11,27),))
                expected=raw['H1'][:];session=cal.daily_session(date(2026,11,27))
                self.assertGreater(expected[-1].scheduled_close,session.end)
                after=expected[-1].scheduled_close
                poison=Candle(after,9000,9999,8000,9500,99999,scheduled_close=after+timedelta(hours=1))
                raw['H1'].append(poison);obs[('H1',poison.time)]=now
                family,status,_=build_family(raw,cal,now,coverage=coverage,observations=obs)
                self.assertEqual(status,'ready')
                for tf in NATIVE_HIGHER:
                    bar=family[tf][-1]
                    self.assertEqual((bar.open,bar.high,bar.low,bar.close,bar.volume),
                        (expected[0].open,max(b.high for b in expected),min(b.low for b in expected),
                         expected[-1].close,sum(b.volume for b in expected)))
                    self.assertEqual(bar.scheduled_close,session.end if tf!='MN1' else
                                     cal.daily_session(date(2026,11,30)).end)

    def test_cold_cache_never_returns_native_prices_for_any_of_the_eight(self):
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=api)
            for symbol in SYMBOLS:
                raw,*_=source(symbol);market._upsert(symbol,'D1',raw['D1'])
                before=market._series(symbol)
                prepared=market.prepare_series(symbol,before,NOW)
                self.assertEqual(prepared.session_metadata['version'],POLICY)
                self.assertTrue(all(not prepared.get(tf) for tf in NATIVE_HIGHER))
                self.assertEqual(market._series(symbol),before)

    def test_different_instruments_keep_separate_coherent_snapshots_after_failure(self):
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=api,clock=lambda:NOW)
            saved={}
            for symbol,price in (('NQZ26@XCME',100),('GCZ26@XCEC',1000)):
                raw,cal,now,obs,coverage=source(symbol,base=price)
                for tf,bars in raw.items():market._upsert(symbol,tf,bars)
                for a,b in coverage['H1']:record_response(market,symbol,'H1',raw['H1'],a,b,now)
                saved[symbol]=market.prepare_series(symbol,market._series(symbol),now)
            with patch('flowdesk.session_store.futures.build_family',return_value=(None,'incomplete_lower_coverage',{})):
                for symbol in saved:
                    result=market.prepare_series(symbol,market._series(symbol),now+timedelta(hours=2))
                    for tf in NATIVE_HIGHER:self.assertEqual(result[tf],saved[symbol][tf])

    def test_native_month_prefix_is_not_mixed_with_new_session_suffix(self):
        for symbol in SYMBOLS:
            raw,cal,now,obs,coverage=source(symbol)
            raw['MN1']=[Candle(datetime(2026,10,1,4,tzinfo=UTC),100,120,80,101,10)]
            family,status,_=build_family(raw,cal,now,coverage=coverage,observations=obs)
            self.assertIsNone(family);self.assertEqual(status,'incomplete_daily_coverage')

    def test_higher_refresh_fetches_hourly_price_source_and_provider_archive_labels(self):
        calls=[]
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
            market_delay_minutes=lambda mic:0,bars=lambda symbol,tf,**kwargs:calls.append((symbol,tf)) or [])
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=api,clock=lambda:NOW)
            for symbol in SYMBOLS:market.refresh_series(symbol,('MN1',))
            for symbol in SYMBOLS:self.assertEqual([tf for s,tf in calls if s==symbol],['H1','MN1'])
            calls.clear();market.refresh_series('SBER@MISX',('MN1',))
            self.assertEqual(calls,[('SBER@MISX','MN1')])

    def test_empty_backfill_keeps_cursor_and_cancellation_makes_no_request(self):
        calls=[]
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,market_delay_minutes=lambda mic:0,
            bars=lambda *args,**kwargs:calls.append(args) or [])
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=api)
            for symbol in ('GCZ26@XCEC','BRNF27@IFEU'):
                raw,*_=source(symbol);market._upsert(symbol,'D1',raw['D1'])
                with self.assertRaises(InterruptedError):backfill(market,symbol,NOW,cancelled=lambda:True)
                self.assertEqual(calls,[])
                backfill(market,symbol,NOW,cancelled=lambda:False)
                self.assertEqual(len(calls),1);self.assertNotIn((symbol,'US_FUTURES_H1_v1'),market.load_history_progress())
                calls.clear()


if __name__=='__main__':unittest.main()
