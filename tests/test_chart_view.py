import unittest
from flowdesk.chart_view import ChartView


class TestChartView(unittest.TestCase):
    def test_zoom_keeps_candle_under_pointer(self):
        view = ChartView(200, 300)
        before = view.window(1000)
        anchor = before[0] + 0.23 * (before[1] - before[0])
        view.zoom(0.82, 0.23, 1000)
        after = view.window(1000)
        self.assertLessEqual(abs(anchor - (after[0] + 0.23 * (after[1] - after[0]))), 0.5)

    def test_zoom_at_right_edge_follows_latest_bar(self):
        view = ChartView()
        view.zoom(0.82, 1, 1000)
        self.assertEqual(view.window(1000)[1], 1000)

    def test_drag_is_bounded_at_both_history_edges(self):
        view = ChartView(100, 0)
        view.pan(5000, 1000)
        self.assertEqual(view.window(1000), (0, 100))
        view.pan(-5000, 1000)
        self.assertEqual(view.window(1000), (900, 1000))

    def test_short_history_and_empty_series_remain_valid(self):
        view = ChartView(125, 50)
        self.assertEqual(view.window(0), (0, 0))
        self.assertEqual(view.window(5), (0, 5))
        view.zoom(0.82, 0.5, 5)
        self.assertEqual(view.window(5), (0, 5))

    def test_right_padding_is_visual_only_and_can_be_dragged(self):
        view = ChartView(count=100, offset=0)
        self.assertEqual(view.right_padding, 16)
        original = view.window(1000)
        view.drag_chart(-30, 1000, start_offset=0, start_padding=16)
        self.assertEqual(view.window(1000), original)
        self.assertEqual(view.right_padding, 46)
        view.drag_chart(20, 1000, start_offset=0, start_padding=46)
        self.assertEqual(view.offset, 20)
        self.assertEqual(view.right_padding, 26)
        view.drag_chart(50, 1000, start_offset=0, start_padding=26)
        self.assertEqual(view.right_padding, 0)
        self.assertEqual(view.offset, 50)

    def test_drag_history_then_future_space_without_synthetic_candles(self):
        view = ChartView(count=50, offset=25, right_padding=12)
        view.drag_chart(-40, 1000, start_offset=25, start_padding=12)
        self.assertEqual(view.offset, 0)
        self.assertEqual(view.right_padding, 27)
        self.assertEqual(view.window(1000), (950, 1000))
        view.drag_chart(-1000, 1000, start_offset=0, start_padding=27)
        self.assertEqual(view.right_padding, 250)

    def test_return_to_latest_preserves_zoom(self):
        view = ChartView(200, 300)
        view.latest()
        self.assertEqual(view.window(1000), (800, 1000))


if __name__ == '__main__':
    unittest.main()
