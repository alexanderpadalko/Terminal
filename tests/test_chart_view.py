import unittest
from flowdesk.chart_view import ChartView, inside_mother_bars, time_axis_zoom_factor
from types import SimpleNamespace


class TestChartView(unittest.TestCase):
    def test_time_axis_factor_is_monotonic_and_safe_across_right_edge(self):
        factors = [time_axis_zoom_factor(500, dx, 1000) for dx in (-500, 0, 100, 400, 500, 900)]
        self.assertEqual(factors, sorted(factors))
        self.assertLess(factors[0], 1)
        self.assertEqual(factors[1], 1)
        self.assertGreater(factors[2], 1)
        self.assertTrue(all(0 < factor <= 50 for factor in factors))

    def test_inside_candles_follow_last_noninside_mother(self):
        def bar(hi, lo):
            return SimpleNamespace(high=hi, low=lo)

        candles = [
            bar(10.5, 8.5),   # first mother
            bar(10.1, 8.8),   # inside mother
            bar(9.9, 9.0),    # nested inside: do not update mother!
            bar(11.0, 9.0),   # breaks old mother, becomes next mother
            bar(10.7, 9.6),   # inside new mother
            bar(10.8, 9.2),   # outside previous inside, but still inside mother
            bar(10.9, 7.9),   # breaks new mother
        ]
        self.assertEqual(inside_mother_bars(candles),
                         [False, True, True, False, True, True, False])

    def test_equal_mother_high_low_counts_as_inside(self):
        def bar(hi, lo):
            return SimpleNamespace(high=hi, low=lo)

        self.assertEqual(
            inside_mother_bars([bar(10, 8), bar(10, 8),
                                bar(10, 9), bar(10.1, 8.1), bar(10.1, 8.1)]),
            [False, True, True, False, True])
        self.assertEqual(inside_mother_bars([]), [])

    def test_zoom_keeps_candle_under_pointer(self):
        view = ChartView(200, 300)
        before = view.bounds(1000)
        anchor = before[0] + 0.23 * view.span
        view.zoom(0.82, 0.23, 1000)
        after = view.bounds(1000)
        self.assertAlmostEqual(anchor, after[0] + 0.23 * view.span, places=10)

    def test_zoom_at_right_edge_follows_latest_bar(self):
        view = ChartView()
        view.zoom(0.82, 1, 1000)
        self.assertEqual(view.window(1000)[1], 1000)

    def test_drag_is_bounded_at_both_history_edges(self):
        view = ChartView(100, 0)
        view.pan(5000, 1000)
        self.assertEqual(view.window(1000)[0], 0)
        view.pan(-5000, 1000)
        self.assertEqual(view.window(1000)[1], 1000)
        self.assertLess(view.bounds(1000)[0], 1000)

    def test_short_history_and_empty_series_remain_valid(self):
        view = ChartView(125, 50)
        self.assertEqual(view.window(0), (0, 0))
        self.assertEqual(view.window(5), (0, 5))
        view.zoom(0.82, 0.5, 5)
        self.assertEqual(view.window(5), (0, 5))

    def test_right_padding_is_visual_only_and_can_be_dragged(self):
        view = ChartView(count=100, offset=0)
        self.assertEqual(view.right_padding, 16)
        original = view.bounds(1000)
        span = view.span
        view.drag_chart(-30, 1000, start_offset=0, start_padding=16)
        self.assertEqual(view.bounds(1000)[0], original[0] + 30)
        self.assertEqual(view.span, span)
        self.assertEqual(view.right_padding, 46)
        view.drag_chart(20, 1000, start_offset=0, start_padding=46)
        self.assertEqual(view.offset, 0)
        self.assertEqual(view.right_padding, 26)
        view.drag_chart(50, 1000, start_offset=0, start_padding=26)
        self.assertEqual(view.right_padding, 0)
        self.assertEqual(view.offset, 24)

    def test_drag_history_then_future_space_without_synthetic_candles(self):
        view = ChartView(count=50, offset=25, right_padding=12)
        view.drag_chart(-40, 1000, start_offset=25, start_padding=12)
        self.assertEqual(view.offset, 0)
        self.assertEqual(view.right_padding, 27)
        self.assertEqual(view.window(1000)[1], 1000)
        view.drag_chart(-1000, 1000, start_offset=0, start_padding=27)
        self.assertEqual(view.right_padding, view.span - 1)

    def test_subbar_drag_is_reversible_and_preserves_pixel_width(self):
        view = ChartView(count=125, offset=40, right_padding=16)
        before = view.bounds(1000)
        spacing = 1000 / view.span
        for dx in (1, 2, 8, 60, -30, 0):
            view.drag_chart(dx / spacing, 1000, start_offset=40, start_padding=16)
            after = view.bounds(1000)
            self.assertAlmostEqual((before[0] - after[0]) * spacing, dx, places=9)
            self.assertEqual(spacing, 1000 / view.span)

    def test_zoom_in_out_preserves_anchor_in_future_whitespace(self):
        view = ChartView(right_padding=65)
        before = view.bounds(1000)
        anchor = before[0] + .87 * view.span
        view.zoom(.82, .87, 1000)
        self.assertAlmostEqual(anchor, view.bounds(1000)[0] + .87 * view.span)
        view.zoom(1 / .82, .87, 1000)
        self.assertAlmostEqual(view.bounds(1000)[0], before[0])
        self.assertAlmostEqual(view.bounds(1000)[1], before[1])

    def test_return_to_latest_preserves_zoom(self):
        view = ChartView(200, 300)
        view.latest()
        self.assertEqual(view.window(1000), (800, 1000))


if __name__ == '__main__':
    unittest.main()

