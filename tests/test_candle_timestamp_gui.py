"""A receipt clock must not become an axis label or the visible footer."""
import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import MainWindow
    from flowdesk.engine import analyze
    from test_gui_navigation import FakeMarket
    QT = True
except ImportError:
    if os.environ.get('CI') == 'true':
        raise
    QT = False

from flowdesk.market_data import Candle, MOSCOW
from flowdesk.chart_time import candle_time_text


@unittest.skipUnless(QT, 'Qt checked in Windows CI')
class TestCandleTimestampGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.w = MainWindow(service=FakeMarket(), watchlist_service=False)
        self.w.resize(1400, 850)
        self.w.show()
        self.app.processEvents()
        self.addCleanup(self.w.close)

    def show_bars(self, last_minute, count=50):
        last = datetime(2026, 11, 12, 11, last_minute, tzinfo=MOSCOW)
        bars = [Candle(last-timedelta(minutes=count-i-1), 10, 12, 9, 11, 100) for i in range(count)]
        self.w.data_info = {'tier': 'network', 'asof': '2026-11-12T11:23:43+03:00',
            'requested_at': '2026-11-12T11:38:43+03:00', 'feed_delay_seconds': 900}
        self.w._show_data(analyze(bars, 'M1'), {}, 'MOEX: SBER / TQBR')
        self.app.processEvents()
        return bars

    def point_to(self, index):
        chart = self.w.chart
        left, _ = chart.viewport.bounds(len(chart.times))
        x = chart.x0 + (index+.5-left) * (chart.x1-chart.x0) / chart.viewport.span
        QTest.mouseMove(chart, QPoint(round(x), round((chart.top+chart.bottom)/2)))
        self.app.processEvents()

    def test_delayed_candle_does_not_display_receipt_clock(self):
        bars = self.show_bars(23)
        self.point_to(len(bars)-1)
        expected = '1м · 12.11.2026 11:23 МСК'
        self.assertEqual(self.w.chart.axis_time_text(), expected)
        self.assertIn(expected, self.w.candle_time.text())
        self.assertNotIn('11:38', self.w.candle_time.text())
        self.assertNotIn('Получено', self.w.data_age.text())
        self.assertNotIn('11:38', self.w.data_age.text())
        self.assertNotIn('15 мин', self.w.data_age.text())
        self.assertIn('11:38:43', self.w.data_age.toolTip())
        # No candle exists in the future whitespace: don't stamp the last
        # candle's time onto an unrelated empty coordinate.
        QTest.mouseMove(self.w.chart, QPoint(round(self.w.chart.x1-1), 100))
        self.app.processEvents()
        self.assertEqual(self.w.chart.axis_time_text(), '')

    def test_current_source_minute_is_not_artificially_shifted_by_delay(self):
        bars = self.show_bars(38)
        self.point_to(len(bars)-1)
        self.assertEqual(self.w.chart.axis_time_text(), '1м · 12.11.2026 11:38 МСК')
        self.assertIn('11:38 МСК', self.w.candle_time.text())
        self.assertEqual(self.w.analysis.candles[-1], bars[-1])

    def test_hover_time_survives_refresh_in_historical_view(self):
        bars = self.show_bars(38)
        chart = self.w.chart
        chart.viewport.count, chart.viewport.offset, chart.viewport.right_padding = 10, 5, 0
        chart.update()
        self.app.processEvents()
        index = chart.window()[0] + 3
        self.point_to(index)
        expected = candle_time_text(bars[index], 'M1')
        self.assertEqual(chart.axis_time_text(), expected)
        newer = bars + [Candle(bars[-1].time+timedelta(minutes=1), 11, 12, 10, 11.5, 100)]
        self.w._show_data(analyze(newer, 'M1'), {}, 'MOEX: SBER / TQBR')
        self.app.processEvents()
        self.assertEqual(chart.axis_time_text(), expected)
        self.assertIn(expected, self.w.candle_time.text())
