"""Native Finam D/W/M policy: history, snapshots, controls and shared consumers."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flowdesk.finam_api import FinamAPI, finam_bar
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle
from flowdesk.provider_candles import native_higher_series, NATIVE_HIGHER, NATIVE_POLICY_VERSION
from flowdesk.settings import IndicatorSettings
from flowdesk.trading_calendar import CalendarUnavailable
from flowdesk.diagnostics_log import log_candle_policy

UTC=timezone.utc
SYMBOL='SBER@MISX'  # Stocks retain native calendar-day D/W/M, including weekends.
NOW=datetime(2026,11,15,16,tzinfo=UTC)


def months():
    return [finam_bar({'timestamp':f'2026-{month:02d}-01T04:00:00Z',
        'open':100+month,'high':110+month,'low':90+month,
        'close':105+month,'volume':1000+month},'MN1') for month in range(6,12)]


class NativeFeed:
    session_candles=True
    native_higher_frames=NATIVE_HIGHER
    stream_frames=FinamAPI.stream_frames
    provider_name='Finam'
    def __init__(self,rows=None):
        self.rows=rows or {};self.calls=[]
    def market_delay_minutes(self,mic):return 0
    def bars(self,symbol,tf,**kwargs):
        self.calls.append((symbol,tf,kwargs))
        return self.rows.get(tf,[])
    def latest_quote(self,symbol):
        return {'symbol':symbol,'last':110,'timestamp':NOW.isoformat()}


class TestNativeHigher(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'data.sqlite'
        self.clock=[NOW]
        self.api=NativeFeed()
        self.market=FinamMarketService(self.path,api=self.api,clock=lambda:self.clock[0])

    def prepare(self):
        return self.market.prepare_series(SYMBOL,self.market._series(SYMBOL),self.clock[0])

    def test_six_native_months_survive_without_lower_bars_or_cme_holiday_calendar(self):
        rows=months();self.market._upsert(SYMBOL,'MN1',rows)
        built=self.prepare()
        self.assertEqual(built['MN1'],rows)
        self.assertEqual(built.session_metadata['version'],NATIVE_POLICY_VERSION)
        self.assertNotIn('MN1',built.session_metadata['issues'])
        self.assertIn('MN1',built.session_metadata['control_issues'])
        main,_,_=self.market.view('SBER','MISX','MN1')
        self.assertEqual(main.candles,rows)
        self.assertEqual(main.confirmed_bars,5)
        self.assertEqual(main.session_info['mode'],'FINAM_NATIVE_CANDLES')
        self.assertTrue(main.session_info['native_higher'])

    def test_unknown_instrument_calendar_cannot_hide_native_higher_frames(self):
        self.market._upsert(SYMBOL,'MN1',months())
        self.market.calendar_factory=lambda symbol:(_ for _ in ()).throw(CalendarUnavailable('instrument_calendar_unknown'))
        built=self.prepare()
        self.assertEqual(built['MN1'],months())
        self.assertEqual(built.session_metadata['issues'],{'H4':'instrument_calendar_unknown'})

    def test_control_source_failure_is_isolated_from_primary_native_history(self):
        self.market._upsert(SYMBOL,'MN1',months())
        bad=Candle(NOW-timedelta(hours=3),100,102,99,101,1,scheduled_close=NOW)
        self.market._upsert(SYMBOL,'H1',[bad])
        built=self.prepare()
        self.assertEqual(built['MN1'],months())
        self.assertEqual(built.session_metadata['issues']['H4'],'control_source_invalid')

    def test_native_d_w_m_are_not_filled_with_control_values(self):
        for tf in NATIVE_HIGHER:
            row=finam_bar({'timestamp':'2026-10-01T04:00:00Z','open':1,'high':3,
                           'low':0,'close':2,'volume':9},tf)
            self.market._upsert(SYMBOL,tf,[row])
        with patch('flowdesk.session_store.build_session_series') as build:
            from flowdesk.session_candles import SessionSeries
            other=Candle(datetime(2026,10,1,tzinfo=UTC),90,99,80,95,8,
                         scheduled_close=datetime(2026,11,1,tzinfo=UTC))
            build.return_value=SessionSeries({tf:[other] for tf in NATIVE_HIGHER})
            built=self.prepare()
        for tf in NATIVE_HIGHER:
            self.assertEqual(len(built[tf]),1)
            self.assertEqual(built[tf][0].close,2)
            self.assertEqual(built.session_metadata['origins'][tf],'finam_native')

    def test_saved_draft_does_not_close_by_clock_alone_but_identical_http_seals_it(self):
        rows=months()[:-1]
        self.clock[0]=datetime(2026,10,15,tzinfo=UTC)
        self.market._upsert(SYMBOL,'MN1',rows,source='stream')
        self.clock[0]=NOW
        built=self.prepare()
        self.assertEqual(built['MN1'],rows[:-1])
        self.assertEqual(built.session_metadata['issues']['MN1'],'native_snapshot_unconfirmed')
        self.market._upsert(SYMBOL,'MN1',rows,source='http')
        self.assertEqual(self.prepare()['MN1'],rows)

    def test_later_forming_snapshot_cannot_leak_into_earlier_asof(self):
        self.clock[0]=NOW+timedelta(days=1)
        self.market._upsert(SYMBOL,'MN1',months())
        self.clock[0]=NOW
        self.assertEqual(self.prepare()['MN1'],months()[:-1])

    def test_old_legacy_archive_is_retained_and_right_edge_revalidated(self):
        rows=months()
        self.market._upsert(SYMBOL,'MN1',rows)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('DELETE FROM candle_source_observations')
        self.assertEqual(self.prepare()['MN1'],rows[:-1])
        self.market._upsert(SYMBOL,'MN1',[rows[-1]],source='http')
        self.assertEqual(self.prepare()['MN1'],rows)

    def test_old_one_month_derived_cache_is_invalidated_and_restart_keeps_six(self):
        rows=months();self.market._upsert(SYMBOL,'MN1',rows)
        self.prepare()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("UPDATE session_series_cache SET policy='previous-session-only',payload=?",
                (json.dumps({'derived':{'MN1':[]},'metadata':{}}),))
        restarted=FinamMarketService(self.path,api=self.api,clock=lambda:NOW)
        self.assertEqual(restarted.prepare_series(SYMBOL,restarted._series(SYMBOL),NOW)['MN1'],rows)
        self.assertEqual(restarted._series(SYMBOL)['MN1'],rows)

    def test_http_requests_native_frame_and_retains_shared_ttl(self):
        self.api.rows={'MN1':months()}
        self.market.refresh_series(SYMBOL,('MN1',))
        self.assertEqual([tf for _,tf,_ in self.api.calls],['MN1'])
        self.assertEqual(self.prepare()['MN1'],months())
        self.market.refresh_series(SYMBOL,('MN1',))
        self.assertEqual(len(self.api.calls),1)
        self.api.h1_only_h4_native_dwm=True
        self.market.refresh_series(SYMBOL,('H4',))
        self.assertEqual([tf for _,tf,_ in self.api.calls][1:],['H1'])

    def test_stream_subscriptions_and_repair_include_native_w_m_not_unused_h4(self):
        self.assertEqual(set(FinamAPI.stream_frames),{'M5','H1','D1','W1','MN1'})
        self.market.mark_stream_reconnect(SYMBOL)
        self.assertIn((SYMBOL,'MN1'),self.market._repair)
        self.assertIn((SYMBOL,'W1'),self.market._repair)
        self.assertNotIn((SYMBOL,'H4'),self.market._repair)

    def test_full_and_overlapping_incremental_native_inputs_match(self):
        rows=months();self.market._upsert(SYMBOL,'MN1',rows)
        other=FinamMarketService(Path(self.temp.name)/'other.sqlite',api=NativeFeed(),clock=lambda:NOW)
        for chunk in (rows[:3],rows[2:5],rows[4:]):other._upsert(SYMBOL,'MN1',chunk)
        self.assertEqual(self.prepare()['MN1'],other.prepare_series(SYMBOL,other._series(SYMBOL),NOW)['MN1'])

    def test_control_diagnostics_are_safe_and_separate_from_primary_failures(self):
        path=Path(self.temp.name)/'diag.log'
        log_candle_policy(symbol=SYMBOL,profile='CME_EQUITY',policy=NATIVE_POLICY_VERSION,
            control_issues={'MN1':'product_holiday_hours_unknown','W1':'SECRET'},counts={'MN1':6},path=path)
        text=path.read_text(encoding='utf-8')
        self.assertIn('MN1_count=6',text)
        self.assertIn('MN1_control=product_holiday_hours_unknown',text)
        self.assertNotIn('SECRET',text)
        self.assertNotIn('MN1=product_holiday_hours_unknown',text)


if __name__=='__main__':unittest.main()
