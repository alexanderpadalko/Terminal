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

    def test_return_to_latest_preserves_zoom(self):
        view = ChartView(200, 300)
        view.latest()
        self.assertEqual(view.window(1000), (800, 1000))


if __name__ == '__main__':
    unittest.main()
