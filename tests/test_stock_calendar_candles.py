"""Stocks keep physical weekend days; only Russian futures regroup DSVD."""
from contextlib import closing
from datetime import date,datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json,tempfile,unittest

from flowdesk.finam_api import finam_bar
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import MOSCOW
from flowdesk import moex_trading_family as moex
from flowdesk.provider_candles import NATIVE_HIGHER,NATIVE_POLICY_VERSION

NOW=datetime(2026,11,16,12,tzinfo=timezone.utc)


def candle(day,tf,prices):
    return finam_bar({'timestamp':day+'T04:00:00Z',
        **dict(zip(('open','high','low','close','volume'),prices))},tf)


class TestStockCalendarCandles(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        logger=patch('flowdesk.diagnostics_log.LOG_PATH',Path(self.directory.name)/'diagnostics.log')
        logger.start();self.addCleanup(logger.stop)
        self.rows={
            'D1':[candle('2026-10-30','D1',(295.05,297.45,294,296.25,14920525)),
                  candle('2026-10-31','D1',(296.25,297,295.17,296.02,1012689)),
                  candle('2026-11-01','D1',(296.03,296.7,295.73,296.7,1001739)),
                  candle('2026-11-02','D1',(296.71,297.37,290,290.8,35321880))],
            'MN1':[candle('1999-06-01','MN1',(1,3,0,2,100)),
                   candle('2026-10-01','MN1',(273.92,300.83,272.91,296.02,552654020)),
                   candle('2026-11-01','MN1',(296.03,300.98,289.05,295.5,226771252))],
            # Synthetic W fixture checks that native W is not rebuilt from a
            # deliberately shorter D sample. D/M boundary prices are audited.
            'W1':[candle('2026-10-26','W1',(295,301,290,296.7,50000000))]}
        self.calls=[]
        def bars(symbol,tf,**kw):self.calls.append((symbol,tf,kw));return self.rows.get(tf,[])
        self.api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
            market_delay_minutes=lambda mic:0,provider_name='Finam',bars=bars)
        self.market=FinamMarketService(Path(self.directory.name)/'cache.sqlite',api=self.api,clock=lambda:NOW)
        for tf,rows in self.rows.items():self.market._upsert('SBER@MISX',tf,rows)

    def prepare(self):return self.market.prepare_series('SBER@MISX',self.market._series('SBER@MISX'),NOW)

    def test_saturday_and_sunday_are_separate_daily_candles_and_october_keeps_saturday_close(self):
        built=self.prepare()
        self.assertEqual(built.session_metadata['version'],NATIVE_POLICY_VERSION)
        for tf in NATIVE_HIGHER:self.assertEqual(built[tf],self.rows[tf])
        self.assertEqual([b.time.astimezone(MOSCOW).date() for b in built['D1']],
            [date(2026,10,30),date(2026,10,31),date(2026,11,1),date(2026,11,2)])
        saturday=built['D1'][1]
        self.assertEqual((saturday.open,saturday.high,saturday.low,saturday.close),(296.25,297,295.17,296.02))
        october=built['MN1'][1]
        self.assertEqual((october.open,october.high,october.low,october.close),(273.92,300.83,272.91,296.02))

    def test_prior_stock_regrouped_cache_is_invalidated_without_changing_raw_or_archive(self):
        raw=self.market._series('SBER@MISX')
        self.market.save_history_progress('SBER@MISX',moex.PROGRESS,{'cursor':datetime(2025,2,24,tzinfo=timezone.utc),'exhausted':True})
        with closing(self.market._connection()) as db,db:
            db.execute('INSERT INTO session_series_cache VALUES(?,?,?,?,?)',('SBER@MISX',
                'obsolete-stock-trading-date-policy',0,NOW.isoformat(),json.dumps({'derived':{},'metadata':{'version':moex.POLICY}})))
        built=self.prepare()
        self.assertEqual(built['MN1'],self.rows['MN1'])
        self.assertEqual(self.market._series('SBER@MISX'),raw)
        self.assertTrue(all(built.session_metadata['origins'][tf]=='finam_native' for tf in NATIVE_HIGHER))

    def test_first_stock_chart_load_does_not_require_futures_bootstrap_or_progress(self):
        with patch('flowdesk.moex_trading_family.backfill',side_effect=AssertionError('stocks must not bootstrap futures history')):
            for tf in NATIVE_HIGHER:
                main,_,_=self.market.view('SBER','MISX',tf,allow_http=True)
                self.assertEqual(main.candles,self.rows[tf])
                self.assertEqual(main.session_info['version'],NATIVE_POLICY_VERSION)
        self.assertNotIn(('SBER@MISX',moex.PROGRESS),self.market.load_history_progress())

    def test_both_stock_mics_are_excluded_and_futures_policy_is_still_enabled(self):
        for symbol in ('SBER@MISX','GAZP@MISX','SBER@XMOE'):
            self.assertFalse(moex.enabled(symbol))
            self.assertIsNone(moex.build_family(self.rows,symbol,NOW,observations={},archive_verified=True)[0])
        self.assertTrue(moex.enabled('CRZ6@RTSX'))
        self.assertTrue(moex.enabled('SiH7@RTSX'))
