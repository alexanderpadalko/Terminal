"""Production Finam source policy: M5/H1/D/W/M native, H4 from H1 alone."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowdesk.finam_api import FinamAPI
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle, MOSCOW
from flowdesk.provider_candles import NATIVE_POLICY_VERSION
from flowdesk.terminal_policy import SOURCE_TIMEFRAMES
from flowdesk import moex_trading_family as moex


UTC=timezone.utc
NOW=datetime(2026,11,18,12,tzinfo=UTC)
START=datetime(2026,11,18,7,tzinfo=MOSCOW).astimezone(UTC)


def candle(ts,ohlcv,end):
    return Candle(ts,*ohlcv,scheduled_close=end)


class DirectFinamFixture:
    session_candles=True
    h1_only_h4_native_dwm=True
    native_higher_frames=('D1','W1','MN1')
    stream_frames=('M5','H1','D1','W1','MN1')
    provider_name='Finam'
    def __init__(self):
        self.calls=[]
        self.rows={}
    def market_delay_minutes(self,mic):
        return 0
    def bars(self,symbol,tf,*,start=None,end=None,now=None):
        self.calls.append((symbol,tf))
        return [b for b in self.rows.get(tf,())
                if (start is None or b.time>=start) and
                   (end is None or b.time<end)]


def native_rows():
    hours=[
        candle(START+timedelta(hours=0),(100,110,98,105,11),START+timedelta(hours=1)),
        candle(START+timedelta(hours=1),(105,120,102,115,12),START+timedelta(hours=2)),
        candle(START+timedelta(hours=2),(115,117,95,105,13),START+timedelta(hours=3)),
        candle(START+timedelta(hours=3),(105,108,100,103,14),START+timedelta(hours=4)),
    ]
    minute=candle(START,(10,999,1,90,999),START+timedelta(minutes=5))
    # Finam's physical Saturday label must not migrate into Monday.
    saturday=datetime(2026,11,14,tzinfo=UTC)
    monday=datetime(2026,11,16,tzinfo=UTC)
    month=datetime(2026,11,1,tzinfo=UTC)
    return {
        'M5':[minute],
        'H1':hours,
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

    def test_production_api_declares_native_sources_and_no_h4_request(self):
        self.assertTrue(FinamAPI.h1_only_h4_native_dwm)
        self.assertEqual(SOURCE_TIMEFRAMES,('M5','H1','D1','W1','MN1'))
        self.service.refresh_series('GNZ6@RTSX',('H4',))
        self.assertEqual([tf for _,tf in self.api.calls],['H1'])
        self.api.calls.clear()
        self.service.refresh_series('GNZ6@RTSX',('D1','W1','MN1'))
        self.assertEqual([tf for _,tf in self.api.calls],['D1','W1','MN1'])

    def test_h4_uses_only_hourly_prices_without_m5_or_native_h4(self):
        symbol='GNZ6@RTSX'
        raw=self.seed(symbol)
        self.service._upsert(symbol,'H4',[candle(START,
            (1,10000,0.1,9999,50000),START+timedelta(hours=4))])
        prepared=self.service.prepare_series(symbol,self.service._series(symbol),NOW)
        self.assertEqual(prepared.session_metadata['version'],NATIVE_POLICY_VERSION)
        self.assertEqual(prepared.session_metadata['origins']['H4'],'finam_h1_only')
        self.assertEqual(len(prepared['H4']),1)
        bar=prepared['H4'][0]
        self.assertEqual((bar.open,bar.high,bar.low,bar.close,bar.volume),
                         (100,120,95,103,50))
        self.assertEqual(bar.time,START)
        self.assertEqual(bar.scheduled_close,START+timedelta(hours=4))
        self.assertEqual(prepared['M5'],raw['M5'])
        self.assertEqual(prepared['H1'],raw['H1'])

    def test_native_daily_weekly_monthly_unchanged_on_moex_and_cme(self):
        for symbol in ('S1Z6@RTSX','GCZ6@XCEC'):
            with self.subTest(symbol=symbol):
                raw=self.seed(symbol)
                result=self.service.prepare_series(symbol,self.service._series(symbol),NOW)
                for tf in ('D1','W1','MN1'):
                    self.assertEqual(result[tf],raw[tf])
                    self.assertEqual(result.session_metadata['origins'][tf],'finam_native')
                self.assertEqual(result['D1'][0].time.weekday(),5)
                self.assertNotIn('moex_staging',result.session_metadata)
                self.assertNotIn('futures_staging',result.session_metadata)

    def test_h4_requires_h1_but_native_dwm_does_not(self):
        symbol='MMZ6@RTSX'
        self.seed(symbol)
        raw=self.service._series(symbol)
        raw['H1']=[]
        prepared=self.service.prepare_series(symbol,raw,NOW)
        self.assertFalse(prepared.get('H4'))
        self.assertEqual(prepared.session_metadata['issues']['H4'],
                         'lower_history_unavailable')
        for tf in ('D1','W1','MN1'):
            self.assertTrue(prepared.get(tf))

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

    def test_h1_chart_uses_normal_view_without_engine_or_other_frames(self):
        symbol='S1Z6@RTSX'
        source=self.seed(symbol)
        with patch('flowdesk.pipeline.analyze',
                   side_effect=AssertionError('technical replay executed')), \
             patch('flowdesk.session_store.prepare',
                   side_effect=AssertionError('H4 aggregation executed')):
            main,parents,info=self.service.view('S1Z6','RTSX','H1',
                                                allow_http=True)
        self.assertEqual(main.timeframe,'H1')
        self.assertEqual(main.candles,source['H1'])
        self.assertEqual((main.segments,main.flows,main.fvg,main.pairs,parents),
                         ([],[],[],[],{}))
        self.assertEqual([tf for _,tf in self.api.calls],['H1'])
        self.assertEqual(info['tf'],'H1')

if __name__=='__main__':
    unittest.main()
