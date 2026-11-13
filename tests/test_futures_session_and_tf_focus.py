"""Gold/COMEX session VWAP and exclusive timeframe highlight, offline Windows CI.

User screenshot: GCZ26@XCEC, M5 checked, M1 retained a focus border,
and gold VWAP incorrectly restarted at civil midnight instead of Globex open.
"""
import os
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flowdesk.chart_indicators import period_key, vwap_series
from flowdesk.exchange_time import exchange_clock
from flowdesk.market_data import Candle

UTC=timezone.utc
NY=ZoneInfo("America/New_York")


def et(day, hour, minute=0, *, month=11):
    return datetime(2026,month,day,hour,minute,tzinfo=NY).astimezone(UTC)


def candle(day, hour, minute, price, *, month=11):
    when=et(day,hour,minute,month=month)
    return Candle(when,price,price,price,price,1)


class TestCommoditySessionVwap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_actual_finam_gold_contract_and_related_venue_contracts(self):
        for mic,ticker in (
            ("XCEC","GCZ26"), ("XCEC","GCZ6"), ("XCEC","GCZ2026"),
            ("XCME","GCZ6"), ("XCEC","SIZ6"), ("XCEC","SIZ2026"),
            ("XNYM","PLF27"), ("XNYM","PAZ6"), ("XNYM","NGZ2026")
        ):
            with self.subTest(mic=mic,ticker=ticker):
                clock=exchange_clock(mic,ticker)
                self.assertTrue(clock.us_globex_commodity_future)
                self.assertFalse(clock.cme_index_future)
                self.assertEqual(clock.zone.key,
                                 "America/Chicago" if mic=="XCME" else "America/New_York")
        for mic,ticker in (("RTSX","SiZ6"),("XCEC","NQZ26"),
                           ("XNYM","GCZ6"),("XCEC","PLF27"),
                           ("XNYM","BRNF27"),("IFEU","BRNF27"),
                           ("XCEC","GCF"),("XCEC","GCZ6X"),
                           ("XCME","MGCZ6"),("XCEC",None)):
            self.assertFalse(exchange_clock(mic,ticker).us_globex_commodity_future,
                             (mic,ticker))

    def test_gc_restarts_at_1800_not_midnight_est(self):
        gold=exchange_clock("XCEC","GCZ26")
        self.assertEqual(period_key(et(12,16,55),"Session",exchange=gold),
                         date(2026,11,12))
        self.assertIsNone(period_key(et(12,17,0),"Session",exchange=gold))
        self.assertIsNone(period_key(et(12,17,55),"Session",exchange=gold))
        for stamp in (et(12,18,0),et(12,23,55),et(13,0,0),et(13,16,55)):
            self.assertEqual(period_key(stamp,"Session",exchange=gold),
                             date(2026,11,13))
        self.assertIsNone(period_key(et(13,17,0),"Session",exchange=gold))
        self.assertIsNone(period_key(et(13,18,0),"Session",exchange=gold))
        self.assertIsNone(period_key(et(14,12,0),"Session",exchange=gold))
        self.assertEqual(period_key(et(15,18,0),"Session",exchange=gold),
                         date(2026,11,16))

    def test_gold_vwap_does_not_reset_at_0000(self):
        bars=[
            candle(12,16,55,100),
            candle(12,17,5,900),
            candle(12,18,0,120),
            candle(12,23,55,130),
            candle(13,0,0,140),  # Screenshot: previously reset here.
            candle(13,16,55,150),
            candle(13,17,30,900),
            candle(15,18,0,200)
        ]
        for mic,ticker in (("XCEC","GCZ6"),("XCEC","SIZ6"),
                           ("XNYM","PLF27"),("XNYM","PAZ6"),("XNYM","NGZ6")):
            with self.subTest(ticker=ticker):
                values,prev=vwap_series(bars,"Session","close",
                                        exchange=exchange_clock(mic,ticker))
                self.assertEqual(values,[100,None,120,125,130,135,None,200])
                self.assertEqual(prev,[[2,7,100],[7,len(bars),135]])
        self.assertEqual(bars[4].time,et(13,0))
        self.assertEqual(bars[4].close,140)

    def test_new_session_from_first_observed_candle_without_fabrication(self):
        bars=[candle(12,16,55,100),candle(12,18,10,200),candle(13,0,0,220)]
        self.assertEqual(vwap_series(bars,"Session","close",
                         exchange=exchange_clock("XCEC","GCZ6"))[0],[100,200,210])

    def test_summer_winter_and_other_anchors(self):
        gold=exchange_clock("XCEC","GCZ6")
        self.assertEqual(et(12,18).hour,23)  # November EST
        self.assertEqual(et(15,18,month=7).hour,22)  # July EDT
        self.assertIsNone(gold.vwap_session_key(et(15,17,59,month=7)))
        self.assertEqual(gold.vwap_session_key(et(15,18,0,month=7)),
                         date(2026,7,16))
        self.assertEqual(period_key(et(12,18),"Month",exchange=gold),
                         period_key(et(13,0),"Month",exchange=gold))
        # Only Session VWAP changes, not Week/Month/Quarter/Year.
        self.assertEqual(period_key(et(12,18),"Week",exchange=gold),
                         period_key(et(13,0),"Week",exchange=gold))
        self.assertIsNone(exchange_clock("XNGS","AAPL").vwap_session_key(et(12,20,0)))

    def test_chart_sets_gc_session_when_instrument_selected(self):
        from flowdesk.engine import analyze
        from flowdesk.gui import CandleChart
        from flowdesk.settings import IndicatorSettings
        bars=[candle(12,16,55,100),candle(12,18,0,120),
              candle(13,0,0,130)]
        chart=CandleChart()
        try:
            chart.set_settings(IndicatorSettings().changed(
                vwapAnchor="Session",vwapSource="close"))
            chart.set_exchange("XCEC","GCZ6")
            chart.set_analysis(analyze(bars,"M5"))
            self.assertTrue(chart.exchange.us_globex_commodity_future)
            self.assertEqual(chart.vwap,[100,120,125])
            # A different, unknown symbol must not inherit Gold's reset.
            chart.set_exchange("XCEC","HGZ6")
            self.assertFalse(chart.exchange.us_globex_commodity_future)
            self.assertEqual(chart.vwap,[100,110,130])
            chart.set_exchange("XCEC","GCZ6")
            self.assertEqual(chart.vwap,[100,120,125])
        finally:
            chart.close()


class TestTimeframeFocus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_only_selected_tf_is_highlighted_after_click_and_restore(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from flowdesk.gui import TimeframeBar
        for theme in ("dark","tv"):
            with self.subTest(theme=theme):
                bar=TimeframeBar()
                try:
                    bar.set_theme(theme)
                    bar.show()
                    self.app.processEvents()
                    QTest.mouseClick(bar.buttons["M1"],Qt.MouseButton.LeftButton)
                    self.assertEqual(bar.currentText(),"M1")
                    bar.setCurrentText("M5")
                    self.app.processEvents()
                    self.assertEqual([tf for tf,b in bar.buttons.items()
                                      if b.isChecked()],["M5"])
                    self.assertTrue(bar.buttons["M5"].isChecked())
                    self.assertFalse(bar.buttons["M1"].isChecked())
                    self.assertEqual(bar.buttons["M1"].focusPolicy(),Qt.FocusPolicy.NoFocus)
                    self.assertFalse(bar.buttons["M1"].hasFocus())
                    self.assertNotIn("QToolButton:focus",bar.styleSheet())
                finally:
                    bar.close()


if __name__=="__main__":
    unittest.main()
