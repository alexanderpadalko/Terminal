"""Real Qt interactions; run on the Windows build runner, without networking."""
import os
import threading
import time
import unittest
from datetime import datetime, timedelta

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import MainWindow
    QT_AVAILABLE = True
except ImportError:
    if os.environ.get('CI') == 'true':
        raise
    QT_AVAILABLE = False

from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW


class FakeMarket:
    def __init__(self, gate=None):
        self.gate, self.calls = gate, []

    def view(self, symbol, board, tf, **kwargs):
        self.calls.append((symbol, board, tf, kwargs['refresh']))
        if self.gate and len(self.calls) == 1:
            self.gate.wait(4)
        start = datetime(2025, 1, 1, tzinfo=MOSCOW)
        delta = {'M1': timedelta(minutes=1), 'M5': timedelta(minutes=5),
                 'H1': timedelta(hours=1), 'D1': timedelta(days=1)}.get(tf, timedelta(days=7))
        bars = [Candle(start + i * delta, 10, 11 + (i % 20) * .1, 9, 10) for i in range(400)]
        a = analyze(bars, tf)
        return a, {}, {'symbol': symbol, 'board': board, 'tf': tf, 'tier': 'memory',
                       'asof': start.isoformat(), 'warning': ''}


@unittest.skipUnless(QT_AVAILABLE, 'Qt is checked in the pinned Windows runtime')
class TestGuiNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.market = FakeMarket()
        self.window = MainWindow(service=self.market)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        if self.market.gate:
            self.market.gate.set()
        self.settled()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def settled(self):
        limit = time.monotonic() + 5
        while self.window.worker is not None and time.monotonic() < limit:
            QTest.qWait(10)
            self.app.processEvents()
        self.assertIsNone(self.window.worker)

    def wheel(self, point, delta=120):
        chart = self.window.chart
        event = QWheelEvent(QPointF(point), QPointF(chart.mapToGlobal(point)), QPoint(), QPoint(0, delta),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(chart, event)
        self.app.processEvents()

    def test_wheel_anchors_visible_candle_under_cursor(self):
        chart = self.window.chart
        chart.viewport.count, chart.offset = 160, 130
        self.app.processEvents()
        x = round(chart.x0 + (chart.x1 - chart.x0) * .3)
        fraction = (x - chart.x0) / (chart.x1 - chart.x0)
        before = chart.window()
        anchor = before[0] + fraction * (before[1] - before[0])
        self.wheel(QPoint(x, 100))
        after = chart.window()
        self.assertLessEqual(abs(anchor - after[0] - fraction * (after[1] - after[0])), .5)

    def test_drag_and_end_key_return_to_latest(self):
        chart = self.window.chart
        center = QPoint(round((chart.x0 + chart.x1) / 2), 120)
        QTest.mousePress(chart, Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseMove(chart, center + QPoint(130, 0))
        QTest.mouseRelease(chart, Qt.MouseButton.LeftButton, pos=center + QPoint(130, 0))
        self.assertGreater(chart.offset, 0)
        QTest.keyClick(chart, Qt.Key.Key_End)
        self.assertEqual(chart.offset, 0)

    def test_price_axis_zoom_and_double_click_reset(self):
        chart = self.window.chart
        before = chart.last_price_range
        self.wheel(QPoint(round(chart.x1 + 25), 150))
        self.assertIsNotNone(chart.price_range)
        self.assertLess(chart.price_range[1] - chart.price_range[0], before[1] - before[0])
        QTest.mouseDClick(chart, Qt.MouseButton.LeftButton, pos=QPoint(round(chart.x1 + 25), 150))
        self.assertIsNone(chart.price_range)

    def test_tf_selection_opens_correct_data_and_preserves_its_view(self):
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        self.settled()
        chart = self.window.chart
        chart.viewport.count, chart.offset = 80, 100
        end = chart.analysis.candles[chart.window()[1] - 1].time
        self.window.tf.setCurrentText('D1')
        self.settled()
        self.assertEqual(self.window.analysis.timeframe, 'D1')
        self.window.tf.setCurrentText('H1')
        self.settled()
        self.assertEqual(self.window.analysis.timeframe, 'H1')
        self.assertEqual(chart.analysis.candles[chart.window()[1] - 1].time, end)
        self.assertEqual(chart.viewport.count, 80)
        self.assertEqual(self.window.tf.currentText(), 'H1')

    def test_rapid_selection_publishes_last_request_without_exporting_old_tf(self):
        self.market.gate = threading.Event()
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        self.window.tf.setCurrentText('M5')
        self.window.tf.setCurrentText('D1')
        self.assertFalse(self.window.export_button.isEnabled())
        self.market.gate.set()
        self.settled()
        self.assertEqual(self.window.analysis.timeframe, 'D1')
        self.assertEqual([x[2] for x in self.market.calls], ['H1', 'D1'])
        self.assertTrue(self.window.export_button.isEnabled())

    def test_labels_use_completed_request_board_not_current_dropdown(self):
        self.market.gate = threading.Event()
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        self.window.board.setCurrentText('TQTF')
        self.market.gate.set()
        self.settled()
        self.assertEqual(self.window.source, 'MOEX: AFKS / TQBR')
        self.assertEqual(self.window.board.currentText(), 'TQBR')

    def test_selection_after_thread_exit_before_finished_delivery_is_queued(self):
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        # The thread has exited, but its queued loaded/finished signals have
        # not reached the GUI. A new selection must not replace that worker.
        self.assertTrue(self.window.worker.wait(3000))
        self.window.tf.setCurrentText('D1')
        self.settled()
        self.assertEqual(self.window.analysis.timeframe, 'D1')
        self.assertEqual([x[2] for x in self.market.calls], ['H1', 'D1'])

    def test_close_during_fetch_does_not_destroy_running_thread(self):
        self.market.gate = threading.Event()
        self.window.load_moex()
        self.window.close()
        self.assertTrue(self.window.closing_after_load)
        self.assertFalse(self.window.isVisible())
        self.market.gate.set()
        self.settled()


if __name__ == '__main__':
    unittest.main()
