"""Windows Qt smoke tests for the fixed TradingView grey-chart/dark-shell UI."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from flowdesk.gui import CHART_PALETTES, CandleChart, MainWindow, demo_data
from flowdesk.engine import analyze


class TestTradingViewAppearance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_fixed_theme_and_hollow_bull_palette(self):
        chart = CandleChart()
        self.assertEqual(chart.visual_theme, "tv")
        self.assertEqual(CHART_PALETTES["tv"]["background"].name(), "#898989")
        self.assertEqual(CHART_PALETTES["tv"]["candle_up_outline"].name(), "#f8f8f8")
        self.assertEqual(CHART_PALETTES["tv"]["candle_down"].name(), "#111111")
        self.assertEqual(chart.viewport.right_padding, 16)
        chart.close()

    def test_visual_controls_preserve_all_candles_and_flows(self):
        analysis = analyze(demo_data(), "H1")
        chart = CandleChart()
        chart.resize(970, 570)
        chart.set_analysis(analysis)
        candles_before = tuple((c.time, c.open, c.high, c.low, c.close, c.volume)
                               for c in analysis.candles)
        flows_before = tuple((f.side, f.lo, f.hi, f.rule) for f in analysis.flows)
        chart.show()
        self.app.processEvents()
        image = chart.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertEqual(image.pixelColor(3, 3).name(), "#898989")
        chart.viewport.drag_chart(-25, len(analysis.candles),
                                 start_offset=0, start_padding=16)
        chart.update()
        self.app.processEvents()
        self.assertEqual(chart.viewport.right_padding, 41)
        self.assertEqual(candles_before, tuple((c.time, c.open, c.high, c.low, c.close, c.volume)
                                                for c in analysis.candles))
        self.assertEqual(flows_before, tuple((f.side, f.lo, f.hi, f.rule) for f in analysis.flows))
        chart.close()

    def test_permanent_dark_chrome_and_instrument_header(self):
        w = MainWindow()
        self.assertEqual(w.chart.visual_theme, "tv")
        self.assertFalse(hasattr(w, "visual_style"))
        self.assertIn("background: #1c1c1c", w.styleSheet())
        self.assertTrue(w.right_panel.isHidden())
        w.panel_checkbox.setChecked(True)
        self.assertFalse(w.right_panel.isHidden())
        w._show_data(analyze(demo_data(), "H1"), {}, "MOEX: AFKS / TQBR")
        self.assertEqual(w.chart.instrument_header, "AFKS · MOEX")
        w.panel_checkbox.setChecked(False)
        self.assertTrue(w.right_panel.isHidden())
        w.close()

    def test_dragging_time_and_price_axes_changes_only_view(self):
        w = MainWindow()
        w.resize(1300, 850)
        w.show()
        self.app.processEvents()
        chart = w.chart
        self.assertGreater(chart.x1, chart.x0)
        old_count = chart.viewport.count
        time_x = (chart.x0 + chart.x1) // 2
        time_y = chart.bottom + 20
        QTest.mousePress(chart, Qt.MouseButton.LeftButton, pos=QPoint(time_x, time_y))
        QTest.mouseMove(chart, QPoint(time_x + 95, time_y))
        QTest.mouseRelease(chart, Qt.MouseButton.LeftButton, pos=QPoint(time_x + 95, time_y))
        self.assertNotEqual(chart.viewport.count, old_count)
        self.app.processEvents()
        y_pos = (chart.top + chart.bottom) // 2
        x_pos = chart.x1 + 25
        QTest.mousePress(chart, Qt.MouseButton.LeftButton, pos=QPoint(x_pos, y_pos))
        QTest.mouseMove(chart, QPoint(x_pos, y_pos + 100))
        QTest.mouseRelease(chart, Qt.MouseButton.LeftButton, pos=QPoint(x_pos, y_pos + 100))
        self.assertIsNotNone(chart.price_range)
        self.assertGreater(chart.price_range[1], chart.price_range[0])
        w.close()

    def test_restore_right_padding_per_view(self):
        chart = CandleChart()
        analysis = analyze(demo_data(), "H1")
        chart.set_analysis(analysis)
        chart.viewport.right_padding = 34
        state = chart.view_state()
        chart.reset_view()
        self.assertEqual(chart.viewport.right_padding, 16)
        chart.restore_view(state)
        self.assertEqual(chart.viewport.right_padding, 34)
        chart.close()


if __name__ == "__main__":
    unittest.main()
