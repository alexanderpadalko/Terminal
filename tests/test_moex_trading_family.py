from contextlib import closing
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3,tempfile,unittest

from flowdesk import moex_trading_family as moex
from flowdesk.chart_axes import time_axis_ticks
from flowdesk.chart_time import candle_time_text
from flowdesk.chart_indicators import vwap_series
from flowdesk.exchange_time import exchange_clock
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle,MOSCOW,serialize_candle
from flowdesk.pipeline import analyze_timeframes
from flowdesk.provider_candles import NATIVE_HIGHER
from flowdesk.session_store import _candle,backfill

UTC=timezone.utc
NOW=datetime(2026,11,16,12,tzinfo=MOSCOW)
SYMBOL='CRZ6@RTSX'

def day(text,o=10,h=12,l=9,c=11,v=100):
    return Candle(datetime.fromisoformat(text).replace(hour=8,tzinfo=MOSCOW),o,h,l,c,v)

def build(rows,now=NOW,symbol=SYMBOL,extra=None,observations=None):
    raw={'D1':rows,**(extra or {})}
    raw.setdefault('H1',[replace(b,time=s.start) for b in rows
                        if (s:=moex._opening_session(b.time.date(),symbol)) is not None])
    known=observations if observations is not None else {(tf,b.time):now for tf,bars in raw.items() for b in bars}
    return moex.build_family(raw,symbol,now,observations=known,archive_verified=True)


class TestMoexTradingFamily(unittest.TestCase):
    def test_weekend_plus_monday_is_one_day_with_each_source_once(self):
        rows=[day('2026-11-13'),day('2026-11-14',11,15,8,12,3),
              day('2026-11-15',12,17,7,13,5),day('2026-11-16',13,14,6,10,7)]
        result,status,_=build(rows)
        self.assertEqual(status,'ready')
        self.assertEqual(len(result['D1']),2)
        bar=result['D1'][-1]
        self.assertEqual((bar.open,bar.high,bar.low,bar.close,bar.volume),(11,17,6,10,15))
        self.assertEqual(bar.trading_date,date(2026,11,16))
        self.assertEqual(bar.time,rows[1].time)
        self.assertEqual(bar.scheduled_close,datetime(2026,11,16,23,50,tzinfo=MOSCOW))
        self.assertEqual(len(rows),4)

    def test_saturday_draft_is_visible_to_analysis_before_its_monday_label(self):
        rows=[day('2026-11-'+str(n)) for n in (10,11,12,13,14)]
        now=NOW.replace(day=14)
        family,status,_=build(rows,now)
        self.assertEqual(status,'ready')
        main,_=analyze_timeframes(family,'D1',asof=now)
        self.assertEqual(main.candles[-1].trading_date,date(2026,11,16))
        self.assertEqual(main.confirmed_bars,4)
        self.assertIn('16.11.2026',candle_time_text(main.candles[-1],'D1',exchange_clock('MISX')))

    def test_month_and_week_boundary_transfer_source_to_new_period(self):
        family,status,_=build([day('2026-10-30'),day('2026-10-31',v=3),day('2026-11-01',v=5),
                              day('2026-11-02',v=7)],NOW)
        self.assertEqual(status,'ready')
        self.assertEqual([(b.trading_date,b.volume) for b in family['MN1']],[(date(2026,10,1),100),(date(2026,11,1),15)])
        self.assertEqual([(b.trading_date,b.volume) for b in family['W1']],[(date(2026,10,26),100),(date(2026,11,2),15)])

    def test_holiday_monday_is_combined_with_following_regular_tuesday(self):
        self.assertEqual(moex.trading_day(date(2026,2,21),SYMBOL),date(2026,2,24))
        self.assertEqual(moex.trading_day(date(2026,2,23),'SIZ6@RTSX'),date(2026,2,24))

    def test_working_saturday_is_not_moved_and_futures_launch_is_respected(self):
        self.assertEqual(moex.trading_day(date(2025,11,1),SYMBOL),date(2025,11,1))
        self.assertEqual(moex.trading_day(date(2025,3,1),SYMBOL),date(2025,3,1))
        self.assertEqual(moex.trading_day(date(2025,3,1),'MXZ6@RTSX'),date(2025,3,1))
        self.assertEqual(moex.trading_day(date(2025,8,16),'MXZ6@RTSX'),date(2025,8,18))

    def test_unknown_or_closed_date_never_invents_a_trading_day(self):
        for value in ('2026-11-28','2027-01-02'):
            family,status,_=build([day(value)],datetime(2027,1,3,tzinfo=MOSCOW))
            self.assertIsNone(family)
            self.assertIn(status,('calendar_source_conflict','calendar_history_unknown'))

    def test_observed_draft_cannot_be_sealed_by_wall_clock(self):
        row=day('2026-11-16')
        known={('D1',row.time):NOW}
        result,status,_=build([row],NOW+timedelta(days=1),observations=known)
        self.assertIsNone(result);self.assertEqual(status,'native_snapshot_unconfirmed')
        result,status,_=build([row],NOW+timedelta(days=1))
        self.assertEqual(status,'ready')

    def test_legacy_source_deadline_is_preserved_in_daily_and_higher_publication(self):
        for stamp in ('2025-08-22','2026-03-20'):
            row=day(stamp)
            row=replace(row,scheduled_close=row.time+timedelta(days=1))
            family,status,_=build([row],symbol='MXZ6@RTSX')
            self.assertEqual(status,'ready')
            for tf in NATIVE_HIGHER:
                self.assertGreaterEqual(family[tf][-1].scheduled_close,row.scheduled_close)
            now=row.time.replace(hour=23,minute=55)
            self.assertGreater(family['D1'][-1].scheduled_close,now)
            self.assertEqual(build([row],now,symbol='MXZ6@RTSX')[0]['D1'][-1].scheduled_close,row.scheduled_close)

    def test_legacy_successor_is_allowed_but_unobserved_right_edge_is_not(self):
        rows=[day('2026-11-12'),day('2026-11-13')]
        self.assertEqual(build(rows,observations={})[1],'native_snapshot_unconfirmed')
        self.assertEqual(build(rows,observations={('D1',rows[-1].time):NOW})[1],'ready')

    def test_archive_prefix_survives_but_missing_affected_month_blocks_promotion(self):
        old=day('1999-06-01',v=999)
        old=replace(old,scheduled_close=datetime(1999,7,1,tzinfo=MOSCOW))
        family,status,_=build([day('2026-11-13')],extra={'MN1':[old]})
        self.assertEqual(status,'ready');self.assertEqual(family['MN1'][0],old)
        self.assertEqual(build([day('2026-11-13')],extra={'MN1':[old,day('2026-10-01')]})[1],'incomplete_daily_coverage')

    def test_initial_short_cache_cannot_masquerade_as_whole_archive(self):
        raw={'D1':[day('2026-11-13')]}
        result,status,_=moex.build_family(raw,SYMBOL,NOW,observations={},archive_verified=False)
        self.assertIsNone(result);self.assertEqual(status,'incomplete_daily_coverage')

    def test_unused_stale_native_month_cannot_erase_the_historical_prefix(self):
        old=replace(day('1999-06-01'),scheduled_close=datetime(1999,7,1,tzinfo=MOSCOW))
        june=day('2026-06-01');november=day('2026-11-01')
        rows=[day('2026-06-02'),day('2026-11-13')]
        known={('D1',b.time):NOW for b in rows}
        known.update({('MN1',old.time):NOW,('MN1',june.time):NOW.replace(month=6),('MN1',november.time):NOW})
        family,status,_=build(rows,extra={'MN1':[old,june,november]},observations=known)
        self.assertEqual(status,'ready');self.assertEqual(family['MN1'][0],old)

    def test_sunday_only_week_disappears_when_its_exact_trades_move_to_monday(self):
        sunday=day('2025-11-09',o=298200,h=300150,l=298200,c=300150,v=2)
        native=replace(sunday,time=day('2025-11-03').time)
        family,status,detail=build([sunday],extra={'W1':[native]},symbol='MXZ6@RTSX')
        self.assertEqual(status,'ready')
        self.assertEqual(family['W1'][0].trading_date,date(2025,11,10))
        self.assertEqual(detail['empty_periods_transferred'],[('W1','2025-11-03')])
        self.assertEqual(build([sunday],extra={'W1':[replace(native,volume=3)]},symbol='MXZ6@RTSX')[1],'incomplete_daily_coverage')

    def test_logical_date_roundtrip_axis_and_vwap_month_anchor(self):
        last=replace(day('2026-10-31',v=10),trading_date=date(2026,11,2),close=12)
        self.assertEqual(_candle(serialize_candle(last)),last)
        prior=day('2026-10-30',c=10,v=100)
        values,_=vwap_series([prior,last],'Month','close',exchange=exchange_clock('MISX'))
        self.assertEqual(values,[10,12])
        ticks=time_axis_ticks([prior,last],1,2,1,200,exchange_clock('MISX').local)
        self.assertEqual(ticks[0][1],'02.11')

    def test_only_supported_moex_symbols_enabled(self):
        for symbol in ('SBER@MISX','SBER@XMOE','SIZ26@XCEC','NQZ26@XCME','BTCUSDT@BINANCE','BAD@RTSX'):
            self.assertFalse(moex.enabled(symbol))
        self.assertTrue(moex.enabled('SiH7@RTSX'))
        for symbol in ('MMZ6@RTSX','GNZ6@RTSX','S1Z6@RTSX',
                       'BMZ6@RTSX','NRX6@RTSX'):
            with self.subTest(symbol=symbol):
                self.assertTrue(moex.enabled(symbol))
                self.assertEqual(moex._opening_session(date(2026,11,18),symbol).segments,
                                 moex._opening_session(date(2026,11,18),'CRZ6@RTSX').segments)
        self.assertFalse(moex.enabled('S1Z6@XCEC'))


class TestMoexTradingStore(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=NOW
        self.api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                                 market_delay_minutes=lambda mic:0,provider_name='Finam')
        self.market=FinamMarketService(Path(self.temp.name)/'cache.sqlite',api=self.api,clock=lambda:self.now)

    def seed(self):
        rows=[day('2026-11-13'),day('2026-11-14'),day('2026-11-15'),day('2026-11-16')]
        self.market._upsert(SYMBOL,'D1',rows)
        self.market._upsert(SYMBOL,'H1',[replace(b,time=moex._opening_session(b.time.date(),SYMBOL).start) for b in rows])
        self.market.save_history_progress(SYMBOL,moex.PROGRESS,{'cursor':datetime(2025,2,24,tzinfo=UTC),'exhausted':True})

    def prepare(self):return self.market.prepare_series(SYMBOL,self.market._series(SYMBOL),self.now)

    def test_cache_restart_raw_unchanged_and_stale_snapshot_retained_safely(self):
        self.seed();raw=self.market._series(SYMBOL);built=self.prepare()
        self.assertEqual(built.session_metadata['version'],moex.POLICY)
        self.assertEqual(self.prepare()['D1'],built['D1'])
        self.assertEqual(self.market._series(SYMBOL),raw)
        self.now+=timedelta(days=1)
        retained=self.prepare()
        self.assertEqual(retained['D1'],built['D1'][:-1])
        self.assertEqual(retained.session_metadata['issues']['D1'],'native_snapshot_unconfirmed')

    def test_native_old_policy_is_invalidated_and_all_three_frames_promoted_together(self):
        self.seed()
        with closing(self.market._connection()) as db,db:
            db.execute('INSERT INTO session_series_cache VALUES(?,?,?,?,?)',(SYMBOL,'old-native',0,NOW.isoformat(),'{}'))
        built=self.prepare()
        self.assertEqual({tf:len(built[tf]) for tf in NATIVE_HIGHER},{'D1':2,'W1':2,'MN1':1})
        self.assertTrue(all(built.session_metadata['origins'][tf]=='finam_moex_trading_day' for tf in NATIVE_HIGHER))

    def test_higher_refresh_requests_daily_and_opening_source_once(self):
        calls=[]
        def bars(symbol,tf,**kw):calls.append(tf);return [day('2026-11-13')]
        self.api.bars=bars
        self.market.refresh_series(SYMBOL,('W1','MN1'))
        self.assertEqual(calls,['H1','D1','W1','MN1'])

    def test_durable_bounded_backfill_and_cancellation(self):
        self.market._upsert(SYMBOL,'MN1',[day('1999-06-01')])
        calls=[]
        def bars(symbol,tf,**kw):calls.append((tf,kw));return []
        self.api.bars=bars
        moex.backfill(self.market,SYMBOL,NOW,cancelled=lambda:False)
        self.assertEqual(len(calls),3)

        self.assertTrue(all(tf=='D1' and kw['end']-kw['start']<=timedelta(days=180) for tf,kw in calls))
        self.assertTrue(self.market.load_history_progress()[(SYMBOL,moex.PROGRESS)]['exhausted'])
        moex.backfill(self.market,SYMBOL,NOW,cancelled=lambda:False)
        self.assertEqual(len(calls),3)
        self.market.save_history_progress(SYMBOL,moex.PROGRESS,{'cursor':NOW,'exhausted':False})
        with self.assertRaises(InterruptedError):moex.backfill(self.market,SYMBOL,NOW,cancelled=lambda:True)
        self.assertEqual(len(calls),3)

    def test_empty_response_cannot_verify_an_existing_price_window(self):
        self.market._upsert(SYMBOL,'D1',[day('2026-11-13')])
        self.api.bars=lambda *args,**kw:[]
        self.assertIn('D1',moex.backfill(self.market,SYMBOL,NOW,cancelled=lambda:False))
        self.assertNotIn((SYMBOL,moex.PROGRESS),self.market.load_history_progress())
