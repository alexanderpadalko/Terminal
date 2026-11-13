"""Observed ruler gestures, exercised through actual Qt mouse events."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import CandleChart, demo_data
    from flowdesk.chart_axes import price_axis_ticks, time_axis_ticks, nice_price_step
    from flowdesk.engine import analyze
    QT_AVAILABLE = True
except ImportError:
    if os.environ.get('CI') == 'true':
        raise
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, 'Real Qt is checked by Windows CI')
class TestRulerGestures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.chart = CandleChart()
        self.chart.resize(1100, 720)
        self.chart.set_analysis(analyze(demo_data(), 'H1'))
        self.chart.show()
        self.app.processEvents()
        self.addCleanup(self.chart.close)

    def drag(self, start, dx=0, dy=0):
        QTest.mousePress(self.chart, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.chart, start + QPoint(dx, dy))
        self.app.processEvents()
        QTest.mouseRelease(self.chart, Qt.MouseButton.LeftButton, pos=start + QPoint(dx, dy))

    def test_chart_recomputes_price_and_time_rulers_during_zoom(self):
        chart=self.chart
        chart.price_range=(75,135)
        n=len(chart.analysis.candles)
        with (patch("flowdesk.gui.price_axis_ticks",wraps=price_axis_ticks) as price,
              patch("flowdesk.gui.time_axis_ticks",wraps=time_axis_ticks) as dates):
            chart.update()
            chart.grab()
            price_before=price.call_args.args
            time_before=dates.call_args.args
            self.assertGreater(len(price_axis_ticks(*price_before[:4])),7)
            chart.price_range=(95,105)
            chart.viewport.zoom(.5,.5,n)
            chart.update()
            chart.grab()
            price_after=price.call_args.args
            time_after=dates.call_args.args
            self.assertLess(nice_price_step(*price_after[:4]),
                            nice_price_step(*price_before[:4]))
            self.assertGreater(time_after[4-1],time_before[4-1])
            self.assertGreaterEqual(price.call_count,2)
            self.assertGreaterEqual(dates.call_count,2)

    def test_price_axis_keeps_center_independent_of_press_height(self):
        chart = self.chart
        results = []
        for height in (.2, .65):
            chart.price_range = (90, 120)
            start = QPoint(round(chart.x1 + 30), round(chart.top + height * (chart.bottom-chart.top)))
            self.drag(start, dy=100)
            lo, hi = chart.price_range
            self.assertAlmostEqual((lo + hi) / 2, 105, places=9)
            self.assertGreater(hi-lo, 30)
            results.append(chart.price_range)
        self.assertEqual(*results)

    def test_time_axis_direction_and_fixed_right_edge_preserve_prices(self):
        chart = self.chart
        bars = tuple(chart.analysis.candles)
        chart.price_range = (90, 120)
        start = QPoint(round(chart.x0 + .55 * (chart.x1-chart.x0)), round(chart.bottom + 20))
        for dx in (100, -100):
            chart.viewport.count, chart.viewport.offset, chart.viewport.right_padding = 125, 100, 16
            right = chart.viewport.bounds(len(chart.times))[1]
            self.drag(start, dx=dx)
            if dx > 0:
                self.assertGreater(chart.viewport.count, 125, 'Right drag must compress candles')
            else:
                self.assertLess(chart.viewport.count, 125, 'Left drag must expand candles')
            self.assertAlmostEqual(chart.viewport.bounds(len(chart.times))[1], right, places=9)
            self.assertEqual(chart.price_range, (90, 120))
            self.assertEqual(tuple(chart.analysis.candles), bars)

    def test_reversing_price_axis_drag_returns_to_original_range(self):
        chart = self.chart
        chart.price_range = (90, 120)
        start = QPoint(round(chart.x1 + 30), round((chart.top+chart.bottom)/2))
        QTest.mousePress(chart, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(chart, start + QPoint(0, 100))
        self.app.processEvents()
        QTest.mouseMove(chart, start)
        self.app.processEvents()
        QTest.mouseRelease(chart, Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(chart.price_range, (90, 120))
