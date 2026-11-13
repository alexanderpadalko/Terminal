"""Regression tests: venue timezone, extended US session VWAP, Moscow midnight.

Pure chart math needs no Finam token and never requests network data.
"""
from datetime import datetime, timezone, timedelta
import os
import unittest

from flowdesk.chart_indicators import period_key, vwap_series
from flowdesk.chart_time import candle_time_text
from flowdesk.exchange_time import exchange_clock
from flowdesk.market_data import Candle

UTC=timezone.utc


def candle(y,m,d,hh,mm,price,volume=1):
    dt=datetime(y,m,d,hh,mm,tzinfo=UTC)
    return Candle(dt,price,price,price,price,volume)


class TestExchangeClockAndVwap(unittest.TestCase):
    def test_exchange_mics_and_unknown_are_not_moscow_by_default(self):
        for mic in ("XNGS","XNMS","XNCM","XNYS","ARCX","XNAS"):
            venue=exchange_clock(mic)
            self.assertEqual(venue.zone.key,"America/New_York",mic)
            self.assertTrue(venue.us_regular_equity)
        self.assertEqual(exchange_clock("MISX").zone.key,"Europe/Moscow")
        self.assertEqual(exchange_clock("XLON").zone.key,"Europe/London")
        self.assertEqual(exchange_clock("XTKS").zone.key,"Asia/Tokyo")
        self.assertFalse(exchange_clock("XXXX").known)
        self.assertEqual(exchange_clock("XXXX").zone.key,"UTC")

    def test_futures_venue_timezones_are_not_utc_or_equity_sessions(self):
        self.assertEqual(exchange_clock("RTSX").zone.key,"Europe/Moscow")
        self.assertEqual(exchange_clock("XCME").zone.key,"America/Chicago")
        self.assertEqual(exchange_clock("XCEC").zone.key,"America/New_York")
        self.assertEqual(exchange_clock("XNYM").zone.key,"America/New_York")
        self.assertEqual(exchange_clock("IFEU").zone.key,"Europe/London")
        self.assertFalse(exchange_clock("XCME").us_regular_equity)
        self.assertFalse(exchange_clock("XNYM").us_regular_equity)

    def test_ny_dst_and_candle_time_are_exchange_local(self):
        venue=exchange_clock("XNGS")
        winter=candle(2026,3,6,14,30,10)
        summer=candle(2026,3,9,13,30,10)
        november=candle(2026,11,13,14,30,10)
        self.assertEqual(candle_time_text(winter,"M1",venue),"1м · 06.03.2026 09:30 EST")
        self.assertEqual(candle_time_text(summer,"M1",venue),"1м · 09.03.2026 09:30 EDT")
        self.assertEqual(candle_time_text(november,"M1",venue),"1м · 13.11.2026 09:30 EST")
        # Legacy MOEX chart remains in Moscow for older GUI regression tests.
        self.assertEqual(candle_time_text(november,"M1"),"1м · 13.11.2026 17:30 МСК")
        self.assertEqual(november.time.tzinfo,UTC)
        self.assertEqual(november.time.hour,14)

    def test_extended_session_includes_pre_regular_post_without_three_resets(self):
        venue=exchange_clock("XNGS")
        instants=[
            (2026,11,12,8,59,False),  # 03:59 ET: before extended session
            (2026,11,12,9,0,True),   # 04:00 ET: premarket open
            (2026,11,12,14,29,True), # 09:29 ET: premarket
            (2026,11,12,14,30,True), # 09:30 ET: core market begins, NO reset
            (2026,11,12,20,59,True), # 15:59 ET: core market
            (2026,11,12,21,0,True),  # 16:00 ET, 00:00 in Moscow, NO reset
            (2026,11,13,0,59,True),  # 19:59 ET: postmarket
            (2026,11,13,1,0,False),  # 20:00 ET: after extended session
            (2026,11,13,9,0,True),   # 04:00 ET: next session starts
        ]
        for y,m,d,hh,mm,expected in instants:
            with self.subTest(hour=f"{d} {hh}:{mm}"):
                self.assertEqual(venue.within_vwap_session(datetime(
                    y,m,d,hh,mm,tzinfo=UTC)),expected)

    def test_session_vwap_accumulates_premarket_core_and_postmarket(self):
        venue=exchange_clock("XNYS")
        bars=[
            candle(2026,11,12,8,59,5),    # outside
            candle(2026,11,12,9,0,100),   # 04 ET
            candle(2026,11,12,14,29,110), # 09:29 ET
            candle(2026,11,12,14,30,120), # 09:30 ET
            candle(2026,11,12,20,59,130), # 15:59 ET
            candle(2026,11,12,21,0,140),  # 16:00 ET / midnight Moscow
            candle(2026,11,13,0,59,150),  # 19:59 ET
            candle(2026,11,13,1,0,500),   # outside
            candle(2026,11,13,9,0,200),   # next day 04:00 ET: reset
            candle(2026,11,13,9,1,210)
        ]
        values,previous=vwap_series(bars,"Session","close",exchange=venue)
        self.assertEqual(values,[None,100,105,110,115,120,125,None,200,205])
        self.assertEqual(previous,[[8,10,125]])
        # No timestamp/OHLC mutations.
        self.assertEqual(bars[5].time,datetime(2026,11,12,21,0,tzinfo=UTC))
        self.assertEqual(bars[5].close,140)

    def test_dst_shift_extended_session_boundary(self):
        venue=exchange_clock("XNGS")
        # Before US daylight-saving switch: 04:00 ET = 09:00 UTC.
        self.assertFalse(venue.within_vwap_session(candle(2026,3,6,8,59,1).time))
        self.assertTrue(venue.within_vwap_session(candle(2026,3,6,9,0,1).time))
        # After US switch: 04:00 ET = 08:00 UTC.
        self.assertFalse(venue.within_vwap_session(candle(2026,3,9,7,59,1).time))
        self.assertTrue(venue.within_vwap_session(candle(2026,3,9,8,0,1).time))

    def test_other_vwap_anchors_follow_exchange_calendar_not_moscow(self):
        us=exchange_clock("XNGS")
        # Moscow already changed date; New York has not.
        earlier=candle(2026,11,12,20,59,10)
        later=candle(2026,11,12,21,0,20)
        self.assertEqual(period_key(earlier.time,"Month",exchange=us),
                         period_key(later.time,"Month",exchange=us))
        self.assertEqual(vwap_series([earlier,later],"Week","close",exchange=us)[0],
                         [10,15])
        # A month boundary in Moscow (31 Oct 21:00 UTC) is not the
        # month boundary in New York: 16:00 ET remains October.
        before=candle(2026,10,31,20,59,10)
        after=candle(2026,10,31,21,1,20)
        self.assertEqual(period_key(before.time,"Month",exchange=us),(2026,10))
        self.assertEqual(period_key(after.time,"Month",exchange=us),(2026,10))


class TestExchangeClockQt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def test_finam_timestamp_labels_use_venue(self):
        from flowdesk.finam_gui import FinamWindow
        self.assertIn("09:30 EST",FinamWindow._exchange_time(
            "2026-11-13T14:30:00Z","XNGS"))
        instant=datetime(2026,11,13,14,30,tzinfo=UTC)
        self.assertIn("09:30:00 EST",FinamWindow._exchange_time(
            {"seconds":int(instant.timestamp()),"nanos":0},"XNGS",seconds=True))

    def test_chart_recalculates_vwap_when_mic_changes(self):
        from flowdesk.gui import CandleChart
        from flowdesk.engine import analyze
        bars=[candle(2026,11,12,9,0,100),candle(2026,11,12,14,30,110),
              candle(2026,11,12,21,0,120)]
        chart=CandleChart()
        try:
            chart.set_exchange("XNGS")
            chart.set_analysis(analyze(bars,"M1"))
            self.assertEqual(chart.vwap[:3],[100,105,110])
            self.assertIn("EST",candle_time_text(bars[1],"M1",chart.exchange))
            chart.set_exchange("MISX")
            self.assertEqual(chart.exchange.zone.key,"Europe/Moscow")
            self.assertNotEqual(chart.vwap[:3],[100,105,110])
        finally:
            chart.close()


if __name__=="__main__":
    unittest.main()
