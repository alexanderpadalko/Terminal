"""Observed ruler gestures, exercised through actual Qt mouse events."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import CandleChart, demo_data
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
