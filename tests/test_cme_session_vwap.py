"""CME NQ/ES Session VWAP resets at 18:00 New York, not civil midnight.

Source: CME Globex index futures schedule, Sunday 18:00 ET to Friday
17:00 ET, daily maintenance 17:00-18:00 ET. All data timestamps remain UTC.
"""
import os
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flowdesk.chart_indicators import period_key, vwap_series
from flowdesk.chart_time import candle_time_text
from flowdesk.exchange_time import exchange_clock
from flowdesk.market_data import Candle

UTC=timezone.utc
ET=ZoneInfo("America/New_York")


def et(y,m,d,h,minute=0):
    return datetime(y,m,d,h,minute,tzinfo=ET).astimezone(UTC)


def candle(y,m,d,h,minute,price):
    start=et(y,m,d,h,minute)
    return Candle(start,price,price,price,price,1)


class TestCmeNqEsSession(unittest.TestCase):
    def setUp(self):
        self.nq=exchange_clock("XCME","NQZ6")
        self.es=exchange_clock("XCME","ESZ2026")

    def test_only_nq_es_contracts_on_cme_receive_the_special_calendar(self):
        for symbol in ("NQZ6","NQZ26","NQZ2026","ESZ6","ESZ2026","NQH27"):
            self.assertTrue(exchange_clock("XCME",symbol).cme_index_future,symbol)
        for mic,ticker in (("XCME","GCZ6"),("XCME","MNQZ6"),
                           ("XCME","MESZ6"),("XCME","NQ"),
                           ("RTSX","NQZ6"),("XCEC","ESZ6"),
                           ("XCME",None)):
            self.assertFalse(exchange_clock(mic,ticker).cme_index_future,(mic,ticker))
        self.assertTrue(exchange_clock("XNGS","NQZ6").us_regular_equity)
        self.assertFalse(exchange_clock("XNGS","NQZ6").cme_index_future)

    def test_maintenance_and_next_trading_date_cross_midnight(self):
        self.assertEqual(period_key(et(2026,11,12,16,55),"Session",exchange=self.nq),
                         date(2026,11,12))
        self.assertIsNone(period_key(et(2026,11,12,17,0),"Session",exchange=self.nq))
        self.assertIsNone(period_key(et(2026,11,12,17,59),"Session",exchange=self.nq))
        for instant in (et(2026,11,12,18,0),et(2026,11,12,23,55),
                        et(2026,11,13,0,0),et(2026,11,13,16,55)):
            self.assertEqual(period_key(instant,"Session",exchange=self.nq),date(2026,11,13))
            self.assertEqual(period_key(instant,"Session",exchange=self.es),date(2026,11,13))
        self.assertIsNone(period_key(et(2026,11,13,17),"Session",exchange=self.nq))
        self.assertIsNone(period_key(et(2026,11,13,18),"Session",exchange=self.nq))
        self.assertIsNone(period_key(et(2026,11,14,10),"Session",exchange=self.nq))
        self.assertIsNone(period_key(et(2026,11,15,17,59),"Session",exchange=self.nq))
        self.assertEqual(period_key(et(2026,11,15,18),"Session",exchange=self.nq),
                         date(2026,11,16))
        self.assertEqual(period_key(et(2026,11,16,0),"Session",exchange=self.nq),
                         date(2026,11,16))

    def test_session_vwap_restarts_once_at_18_no_reset_at_midnight(self):
        bars=[
            candle(2026,11,12,16,55,100),
            candle(2026,11,12,17,0,900),
            candle(2026,11,12,17,59,900),
            candle(2026,11,12,18,0,120),
            candle(2026,11,12,23,55,130),
            candle(2026,11,13,0,0,140),
            candle(2026,11,13,16,55,150),
            candle(2026,11,13,17,0,900),
            candle(2026,11,15,17,59,900),
            candle(2026,11,15,18,0,200),
            candle(2026,11,16,0,0,220),
        ]
        for clock in (self.nq,self.es):
            with self.subTest(symbol=clock):
                value,previous=vwap_series(bars,"Session","close",exchange=clock)
                self.assertEqual(value,[100,None,None,120,125,130,135,None,None,200,210])
                self.assertEqual(previous,[[3,9,100],[9,len(bars),135]])
        self.assertEqual(bars[3].time,et(2026,11,12,18))
        self.assertEqual(bars[3].close,120)

    def test_missing_1800_bar_still_starts_new_session_at_first_real_trade(self):
        bars=[candle(2026,11,12,16,55,100),
              candle(2026,11,12,18,5,200),
              candle(2026,11,12,18,10,220)]
        self.assertEqual(vwap_series(bars,"Session","close",exchange=self.nq)[0],
                         [100,200,210])

    def test_dst_changes_utc_boundary_while_chart_uses_chicago(self):
        self.assertEqual(et(2026,11,12,18).hour,23)  # EST: 18 -> 23 UTC
        self.assertEqual(et(2026,7,15,18).hour,22)   # EDT: 18 -> 22 UTC
        for year,month,day in ((2026,7,15),(2026,11,12)):
            self.assertIsNone(self.nq.vwap_session_key(et(year,month,day,17,59)))
            self.assertEqual(self.nq.vwap_session_key(et(year,month,day,18,0)),
                             date(year,month,day+1))
        self.assertIn("17:00 CST",candle_time_text(
            Candle(et(2026,11,12,18),1,1,1,1,1),"M5",self.nq))
        self.assertIn("17:00 CDT",candle_time_text(
            Candle(et(2026,7,15,18),1,1,1,1,1),"M5",self.nq))

    def test_other_anchors_and_instruments_remain_unchanged(self):
        before=et(2026,11,12,18)
        midnight=et(2026,11,13,0)
        self.assertEqual(period_key(before,"Week",exchange=self.nq),
                         period_key(midnight,"Week",exchange=self.nq))
        self.assertEqual(period_key(before,"Month",exchange=self.nq),
                         period_key(midnight,"Month",exchange=self.nq))
        generic=exchange_clock("XCME","GCZ6")
        self.assertEqual(period_key(before,"Session",exchange=generic),
                         date(2026,11,12))  # no invented gold session times
        equity=exchange_clock("XNGS","AAPL")
        self.assertIsNone(period_key(before,"Session",exchange=equity))
        self.assertEqual(equity.vwap_session_key(et(2026,11,12,16,55)),
                         date(2026,11,12))

    def test_naive_futures_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            self.nq.vwap_session_key(datetime(2026,11,12,18,0))


class TestCmeChartIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_chart_session_follows_selected_finam_symbol(self):
        from flowdesk.gui import CandleChart
        from flowdesk.engine import analyze
        from flowdesk.settings import IndicatorSettings
        bars=[candle(2026,11,12,16,55,100),
              candle(2026,11,12,18,0,120),
              candle(2026,11,13,1,0,130)]  # 00:00 Chicago, still same Globex session
        chart=CandleChart()
        try:
            chart.set_settings(IndicatorSettings().changed(vwapAnchor="Session",
                                                            vwapSource="close"))
            chart.set_exchange("XCME","NQZ6")
            chart.set_analysis(analyze(bars,"M5"))
            self.assertEqual(chart.vwap,[100,120,125])
            self.assertTrue(chart.exchange.cme_index_future)
            chart.set_exchange("XCME","ESZ6")
            self.assertEqual(chart.vwap,[100,120,125])
            chart.set_exchange("XCME","GCZ6")
            self.assertFalse(chart.exchange.cme_index_future)
            self.assertEqual(chart.vwap,[100,110,130])
            chart.set_exchange("XCME","NQZ6")
            self.assertEqual(chart.vwap,[100,120,125])
        finally:
            chart.close()


if __name__=="__main__":
    unittest.main()
