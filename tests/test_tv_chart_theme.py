"""Windows Qt smoke tests for the fixed TradingView grey-chart/dark-shell UI."""
import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from flowdesk.gui import CHART_PALETTES, CandleChart, MainWindow, demo_data
from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW
from flowdesk.settings import ApplicationSettings, IndicatorSettings


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

    def test_only_inside_candles_are_hollow_in_rendered_pixels(self):
        # Positive AND negative inside candles must stay transparent.
        # Non-inside bullish candles must be solid white; bearish solid black.
        start = datetime(2026, 11, 11, 7, tzinfo=MOSCOW)
        ohlc = [
            (9.0, 10.5, 8.5, 10.0),  # bullish mother, solid white
            (9.5, 10.1, 8.8, 9.3),  # bearish inside, transparent
            (9.1, 9.9, 9.0, 9.8),   # bullish inside, transparent
            (9.2, 11.0, 9.0, 10.8), # new bullish mother, solid white
            (10.6, 10.7, 9.6, 10.2),# bearish inside, transparent
            (10.3, 10.8, 9.2, 9.5), # still inside mother, transparent
            (10.0, 10.9, 7.9, 8.4), # bearish breakout, solid black
        ]
        bars = [Candle(start + timedelta(hours=i), op, hi, lo, cl, 1000)
                for i, (op, hi, lo, cl) in enumerate(ohlc)]
        chart = CandleChart()
        chart.resize(1100, 720)
        chart.set_analysis(analyze(bars, "H1"))
        chart.viewport.count = 7
        chart.viewport.right_padding = 0
        chart.price_range = (7.0, 12.0)
        chart.show_structure = chart.show_fvg = chart.show_flow = False
        chart.show_previous = False
        chart.show()
        self.app.processEvents()
        image = chart.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertEqual(chart.inside_bars, [False, True, True, False, True, True, False])
        left, _ = chart.viewport.bounds(len(bars))
        stride = (chart.x1 - chart.x0) / chart.viewport.span

        def body_color(i, x_offset=5):
            candle = bars[i]
            mid = (candle.open + candle.close) / 2
            y = round(chart.bottom - (mid - 7.0) / 5.0 * (chart.bottom - chart.top))
            x = round(chart.x0 + (i + 0.5 - left) * stride + x_offset)
            return image.pixelColor(x, y).name()

        self.assertEqual(body_color(0), "#ffffff")
        self.assertEqual(body_color(1), "#898989")
        self.assertEqual(body_color(2), "#898989")
        self.assertEqual(body_color(3), "#ffffff")
        self.assertEqual(body_color(4), "#898989")
        self.assertEqual(body_color(5), "#898989")
        self.assertEqual(body_color(6), "#111111")
        # At the candle CENTER the wick used to remain visible through the
        # transparent hollow body. It must now be pure chart background.
        for index in (1, 2, 4, 5):
            with self.subTest(inside_index=index):
                pixel = body_color(index, x_offset=0)
                # The antialiased pale grid can tint the chart background by
                # 2-3 RGB units at this pixel. A wick passing through the body
                # would instead produce a near-white/black stroke (>80 away).
                channel = int(pixel[1:3], 16)
                self.assertLessEqual(abs(channel - 0x89), 8, pixel)
                self.assertEqual(pixel[1:3], pixel[3:5])
                self.assertEqual(pixel[3:5], pixel[5:7])
        chart.close()

    def test_hollow_inside_is_visible_in_finam_dark_theme_and_toggle_repaints(self):
        start=datetime(2026,11,11,7,tzinfo=MOSCOW)
        ohlc=[
            (9,11,8,10),         # solid bullish mother
            (9.6,10.5,8.8,9.1), # hollow bearish inside
            (9.2,10.1,8.9,9.9), # hollow bullish inside
            (10,12,7,8),        # solid bearish breakout
        ]
        bars=[Candle(start+timedelta(hours=i),op,hi,lo,cl,1000)
              for i,(op,hi,lo,cl) in enumerate(ohlc)]
        chart=CandleChart()
        chart.resize(1100,720)
        chart.set_theme('dark')
        chart.set_settings(IndicatorSettings().changed(ibEnabled=True))
        chart.set_chart_colors(ApplicationSettings(
            chart_candle_up='#11BB44FF',
            chart_candle_down='#DD3355FF',
            chart_outline_up='#FAFAFAFF',
            chart_outline_down='#FFFF00FF',
            chart_wick_up='#EEEEEEFF',
            chart_wick_down='#EEEEEEFF',
            chart_background='#161616FF',
            chart_grid='#00000000'))
        chart.set_analysis(analyze(bars,'H1'))
        chart.viewport.count=4
        chart.viewport.right_padding=0
        chart.price_range=(6.0,13.0)
        chart.show_structure=chart.show_fvg=chart.show_flow=False
        chart.show_previous=False
        chart.show()
        self.app.processEvents()
        self.assertEqual(chart.inside_bars,[False,True,True,False])

        def sample(i):
            picture=chart.grab().toImage()
            left,_=chart.viewport.bounds(len(bars))
            stride=(chart.x1-chart.x0)/chart.viewport.span
            # +/-5 px is safely inside the body, not on the wick/outline.
            px=round(chart.x0+(i+0.5-left)*stride+5)
            center=(bars[i].open+bars[i].close)/2
            py=round(chart.bottom-(center-6)/7*(chart.bottom-chart.top))
            return picture.pixelColor(px,py).name()

        self.assertEqual(sample(0),'#11bb44')
        self.assertEqual(sample(1),'#161616')
        self.assertEqual(sample(2),'#161616')
        self.assertEqual(sample(3),'#dd3355')
        chart.set_settings(IndicatorSettings().changed(ibEnabled=False))
        self.app.processEvents()
        self.assertEqual(sample(1),'#dd3355')
        self.assertEqual(sample(2),'#11bb44')
        chart.set_settings(IndicatorSettings().changed(ibEnabled=True))
        self.app.processEvents()
        self.assertEqual(sample(1),'#161616')
        self.assertEqual(sample(2),'#161616')
        chart.close()

    def test_dragging_plot_translates_x_and_y_without_rescaling_price(self):
        w = MainWindow(startup=False, watchlist_service=False)
        w.resize(1300, 850)
        w.show()
        self.app.processEvents()
        chart = w.chart
        chart.viewport.count = 125
        chart.viewport.offset = 90
        chart.update()
        self.app.processEvents()
        start_offset = chart.offset
        start_padding = chart.viewport.right_padding
        start_range = chart.last_price_range
        self.assertIsNotNone(start_range)
        start_span = start_range[1] - start_range[0]
        start = QPoint(round((chart.x0 + chart.x1) / 2),
                       round((chart.top + chart.bottom) / 2))
        dx, dy = 112, 64
        QTest.mousePress(chart, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(chart, start + QPoint(dx, dy))
        self.app.processEvents()
        QTest.mouseRelease(chart, Qt.MouseButton.LeftButton,
                           pos=start + QPoint(dx, dy))
        self.assertGreater(chart.offset, start_offset)
        self.assertLess(chart.viewport.right_padding, start_padding)
        self.assertIsNotNone(chart.price_range)
        lo, hi = chart.price_range
        self.assertAlmostEqual(hi - lo, start_span, delta=1e-8)
        self.assertAlmostEqual(
            lo - start_range[0],
            dy * start_span / (chart.bottom - chart.top), delta=1e-7)
        # A further horizontal-only pan must preserve the Y range even
        # after the visible candle window changes.
        w.close()

    def test_one_pixel_drag_tracks_mouse_without_changing_candle_spacing(self):
        chart = CandleChart()
        chart.resize(970, 600)
        chart.set_analysis(analyze(demo_data(), 'H1'))
        chart.viewport.offset = 90
        chart.show()
        self.app.processEvents()
        before = chart.viewport.bounds(len(chart.times))
        spacing = (chart.x1 - chart.x0) / chart.viewport.span
        start = QPoint(400, 200)
        QTest.mousePress(chart, Qt.MouseButton.LeftButton, pos=start)
        for dx in (1, 2, 30, -8, 0):
            QTest.mouseMove(chart, start + QPoint(dx, 0))
            self.app.processEvents()
            after = chart.viewport.bounds(len(chart.times))
            self.assertAlmostEqual((before[0] - after[0]) * spacing, dx, places=7)
            self.assertAlmostEqual(spacing, (chart.x1 - chart.x0) / chart.viewport.span)
        QTest.mouseRelease(chart, Qt.MouseButton.LeftButton, pos=start)
        chart.close()

    def test_permanent_dark_chrome_and_instrument_header(self):
        w = MainWindow(startup=False, watchlist_service=False)
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
        w = MainWindow(startup=False, watchlist_service=False)
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

