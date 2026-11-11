"""UI-only TradingView theme regressions; no exchange data and no orders."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from flowdesk.gui import CHART_PALETTES, CandleChart, MainWindow, demo_data
from flowdesk.engine import analyze


class TestTVChartTheme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_palette_and_default(self):
        chart = CandleChart()
        self.assertEqual(chart.visual_theme, "tv")
        self.assertEqual(CHART_PALETTES["tv"]["background"].name(), "#898989")
        self.assertEqual(CHART_PALETTES["tv"]["candle_up"].name(), "#ffffff")
        self.assertEqual(CHART_PALETTES["tv"]["candle_down"].name(), "#111111")
        with self.assertRaises(ValueError):
            chart.set_theme("unknown")
        chart.close()

    def test_both_modes_render_without_calculation_change(self):
        analysis = analyze(demo_data(), "H1")
        chart = CandleChart()
        chart.resize(970, 570)
        chart.set_analysis(analysis)
        candles_before = tuple((c.time, c.open, c.high, c.low, c.close, c.volume)
                               for c in analysis.candles)
        flows_before = tuple((f.side, f.lo, f.hi, f.rule) for f in analysis.flows)
        for theme, background in (("tv", "#898989"), ("dark", "#0c131e")):
            chart.set_theme(theme)
            chart.show()
            self.app.processEvents()
            image = chart.grab().toImage()
            self.assertFalse(image.isNull())
            self.assertEqual(image.pixelColor(3, 3).name(), background)
        self.assertEqual(candles_before, tuple((c.time, c.open, c.high, c.low, c.close, c.volume)
                                                for c in analysis.candles))
        self.assertEqual(flows_before, tuple((f.side, f.lo, f.hi, f.rule) for f in analysis.flows))
        chart.close()

    def test_sidebar_and_theme_switcher(self):
        w = MainWindow()
        self.assertEqual(w.chart.visual_theme, "tv")
        self.assertTrue(w.right_panel.isHidden())
        w.panel_checkbox.setChecked(True)
        self.assertFalse(w.right_panel.isHidden())
        w.visual_style.setCurrentIndex(1)
        self.assertEqual(w.chart.visual_theme, "dark")
        w.visual_style.setCurrentIndex(0)
        self.assertEqual(w.chart.visual_theme, "tv")
        self.assertTrue(w.chart.show_fvg)
        w.close()


if __name__ == "__main__":
    unittest.main()
