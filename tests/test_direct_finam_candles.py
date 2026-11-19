"""Production Finam source policy: native M5/H4/D/W/M; H1 is retired."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowdesk.finam_api import FinamAPI
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle, MOSCOW
from flowdesk.provider_candles import NATIVE_ALL_POLICY_VERSION
from flowdesk.terminal_policy import SOURCE_TIMEFRAMES, DISPLAY_TIMEFRAMES
from flowdesk import moex_trading_family as moex

UTC=timezone.utc
NOW=datetime(2026,11,18,12,tzinfo=UTC)
START=datetime(2026,11,18,7,tzinfo=MOSCOW).astimezone(UTC)


def candle(ts,ohlcv,end):
    return Candle(ts,*ohlcv,scheduled_close=end)


class DirectFinamFixture:
    session_candles=True
    native_all_frames=True
    h1_only_h4_native_dwm=False
    native_higher_frames=('D1','W1','MN1')
    stream_frames=('M5','H4','D1','W1','MN1')
    provider_name='Finam'
    def __init__(self):
        self.calls=[]
        self.rows={}
    def market_delay_minutes(self,mic):
        return 0
    def bars(self,symbol,tf,*,start=None,end=None,now=None):
        self.calls.append((symbol,tf))
        return [bar for bar in self.rows.get(tf,())
                if (start is None or bar.time>=start) and
                   (end is None or bar.time<end)]


def native_rows():
    saturday=datetime(2026,11,14,tzinfo=UTC)
    monday=datetime(2026,11,16,tzinfo=UTC)
    month=datetime(2026,11,1,tzinfo=UTC)
    return {
        'M5':[candle(START,(10,999,1,90,999),START+timedelta(minutes=5))],
        'H4':[candle(START,(100,120,95,103,50),START+timedelta(hours=4))],
        'D1':[candle(saturday,(200,230,170,220,500),saturday+timedelta(days=1))],
        'W1':[candle(monday,(300,360,270,340,600),monday+timedelta(days=7))],
        'MN1':[candle(month,(400,470,370,440,700),datetime(2026,12,1,tzinfo=UTC))]
    }


class DirectFinamSourceTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.api=DirectFinamFixture()
        self.service=FinamMarketService(Path(directory.name)/'candles.sqlite',
                                        api=self.api,clock=lambda:NOW)
    def seed(self,symbol):
        rows=native_rows()
        for tf,items in rows.items():
            self.service._upsert(symbol,tf,items)
        return rows

    def test_production_declares_five_native_sources_and_retires_h1(self):
        self.assertTrue(FinamAPI.native_all_frames)
        self.assertFalse(FinamAPI.h1_only_h4_native_dwm)
        self.assertEqual(SOURCE_TIMEFRAMES,('M5','H4','D1','W1','MN1'))
        self.assertEqual(DISPLAY_TIMEFRAMES,SOURCE_TIMEFRAMES)
        self.assertEqual(FinamAPI.stream_frames,SOURCE_TIMEFRAMES)
        self.service.refresh_series('GNZ6@RTSX',('H1','H4'))
        self.assertEqual([tf for _,tf in self.api.calls],['H4'])
        self.api.calls.clear()
        self.service.refresh_series('GNZ6@RTSX',('D1','W1','MN1'))
        self.assertEqual([tf for _,tf in self.api.calls],['D1','W1','MN1'])

    def test_h4_is_exact_provider_ohlcv_not_m5_h1_or_old_reconstructed_h4(self):
        symbol='GNZ6@RTSX'
        raw=self.seed(symbol)
        self.service._upsert(symbol,'H1',[candle(START,
            (1,10000,0.1,9999,50000),START+timedelta(hours=1))])
        with patch.object(self.service,'calendar_factory',
                          side_effect=AssertionError('calendar must not run')), \
             patch('flowdesk.session_candles.build_h4_from_finam_h1',
                   side_effect=AssertionError('derived H4 must not run')):
            series=self.service.prepare_series(symbol,self.service._series(symbol),NOW)
        self.assertEqual(series.session_metadata['version'],NATIVE_ALL_POLICY_VERSION)
        self.assertEqual(series.session_metadata['origins']['H4'],'finam_native')
        self.assertEqual(series['H4'],raw['H4'])
        self.assertEqual(series['M5'],raw['M5'])
        self.assertNotIn('H1',self.service._series(symbol))
        self.assertNotIn('H1',series)

    def test_h4_is_independent_of_missing_h1_and_dwm_are_original(self):
        for symbol in ('S1Z6@RTSX','GCZ6@XCEC','SBER@MISX'):
            with self.subTest(symbol=symbol):
                raw=self.seed(symbol)
                result=self.service.prepare_series(symbol,self.service._series(symbol),NOW)
                for tf in SOURCE_TIMEFRAMES:
                    self.assertEqual(result[tf],raw[tf])
                    self.assertEqual(result.session_metadata['origins'][tf],'finam_native')
                self.assertEqual(result['D1'][0].time.weekday(),5)
                self.assertNotIn('moex_staging',result.session_metadata)
                self.assertNotIn('futures_staging',result.session_metadata)

    def test_provider_h4_requires_postclose_evidence_not_an_invented_candle(self):
        from contextlib import closing
        symbol='MMZ6@RTSX'
        self.seed(symbol)
        stamp=native_rows()['H4'][0].time
        with closing(self.service._connection()) as db,db:
            db.execute('DELETE FROM candle_source_observations WHERE symbol=? AND tf=?',
                       (symbol,'H4'))
        series=self.service.prepare_series(symbol,self.service._series(symbol),NOW)
        self.assertNotIn('H4',series)
        self.assertEqual(series.session_metadata['issues']['H4'],
                         'native_snapshot_unconfirmed')
        self.service.refresh_series(symbol,('H4',))
        # A successful provider fetch records evidence; no H1 request.
        self.assertEqual([tf for _,tf in self.api.calls],['H4'])

    def test_stock_m5_auction_remains_native(self):
        symbol='SBER@MISX'
        stamp=datetime(2026,11,18,18,55,tzinfo=MOSCOW).astimezone(UTC)
        now=datetime(2026,11,18,20,tzinfo=UTC)
        stock=FinamMarketService(self.service.path.with_name('stock.sqlite'),
                                 api=DirectFinamFixture(),clock=lambda:now)
        native=candle(stamp,(10,20,9,19,90),stamp+timedelta(minutes=5))
        stock._upsert(symbol,'M5',[native])
        result=stock.prepare_series(symbol,stock._series(symbol),now)
        self.assertEqual(result['M5'],[native])

    def test_h1_chart_is_rejected_and_h4_uses_source_in_normal_view(self):
        symbol='S1Z6@RTSX'
        source=self.seed(symbol)
        self.api.rows=native_rows()
        with self.assertRaises(ValueError):
            self.service.view('S1Z6','RTSX','H1',allow_http=True)
        with patch('flowdesk.session_candles.build_h4_from_finam_h1',
                   side_effect=AssertionError('H4 reconstruction ran')):
            main,parents,info=self.service.view('S1Z6','RTSX','H4',allow_http=True)
        self.assertEqual(main.timeframe,'H4')
        self.assertEqual(main.candles,source['H4'])
        self.assertFalse(any(tf=='H1' for _,tf in self.api.calls))
        self.assertEqual(info['tf'],'H4')

if __name__=='__main__':
    unittest.main()
