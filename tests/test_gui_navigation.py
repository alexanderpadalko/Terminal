"""Real Qt interactions; run on the Windows build runner, without networking."""
import os
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
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
        self.gate, self.calls, self.error = gate, [], None

    def view(self, symbol, board, tf, **kwargs):
        self.calls.append((symbol, board, tf, kwargs['refresh']))
        if self.gate and len(self.calls) == 1:
            self.gate.wait(4)
        if self.error:
            raise ConnectionError(self.error)
        start = datetime(2025, 1, 1, tzinfo=MOSCOW)
        delta = {'M1': timedelta(minutes=1), 'M5': timedelta(minutes=5),
                 'H1': timedelta(hours=1), 'H4': timedelta(hours=4),
                 'D1': timedelta(days=1)}.get(tf, timedelta(days=7))
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
        self.window.http_checkbox.setChecked(True)
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
        before = chart.viewport.bounds(len(chart.times))
        anchor = before[0] + fraction * chart.viewport.span
        self.wheel(QPoint(x, 100))
        after = chart.viewport.bounds(len(chart.times))
        self.assertAlmostEqual(anchor, after[0] + fraction * chart.viewport.span, places=9)

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

    def test_interval_buttons_select_h4_and_remain_exclusive(self):
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        self.settled()
        bar = self.window.tf
        self.assertEqual([b.text() for b in bar.buttons.values()], ['1м', '5м', '1ч', '4ч', 'Д', 'Н', 'М'])
        QTest.mouseClick(bar.buttons['H4'], Qt.MouseButton.LeftButton)
        self.settled()
        self.assertEqual(self.window.analysis.timeframe, 'H4')
        self.assertEqual(bar.currentText(), 'H4')
        self.assertEqual([tf for tf, b in bar.buttons.items() if b.isChecked()], ['H4'])
        calls = len(self.market.calls)
        QTest.mouseClick(bar.buttons['H4'], Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(len(self.market.calls), calls)
        QTest.mouseClick(bar.buttons['M5'], Qt.MouseButton.LeftButton)
        self.settled()
        self.assertEqual(self.window.analysis.timeframe, 'M5')
        self.assertEqual([tf for tf, b in bar.buttons.items() if b.isChecked()], ['M5'])

    def test_programmatic_interval_sync_does_not_fetch(self):
        bar = self.window.tf
        bar.blockSignals(True)
        bar.setCurrentText('H4')
        bar.blockSignals(False)
        self.app.processEvents()
        self.assertEqual(bar.currentText(), 'H4')
        self.assertTrue(bar.buttons['H4'].isChecked())
        self.assertEqual(self.market.calls, [])

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
        self.assertFalse(self.window.auto_timer.isActive())
        self.market.gate.set()
        self.settled()

    def test_real_timer_automatically_refreshes_loaded_instrument(self):
        self.window.auto_timer.setInterval(30)
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        self.settled()
        deadline = time.monotonic() + 3
        while len(self.market.calls) < 2 and time.monotonic() < deadline:
            QTest.qWait(10)
        self.window.auto_checkbox.setChecked(False)
        self.settled()
        self.assertGreaterEqual(len(self.market.calls), 2)
        self.assertTrue(all(c[:3] == ('AFKS', 'TQBR', 'H1') and c[3] for c in self.market.calls))
        self.assertFalse(self.window.auto_timer.isActive())

    def test_auto_uses_selected_tf_and_can_pause_and_resume(self):
        self.window.load_moex()
        self.settled()
        self.window.tf.setCurrentText('D1')
        self.settled()
        self.window.auto_checkbox.setChecked(False)
        count = len(self.market.calls)
        self.window._auto_refresh()
        self.assertEqual(len(self.market.calls), count)
        self.assertFalse(self.window.auto_timer.isActive())
        self.window.auto_checkbox.setChecked(True)
        self.assertTrue(self.window.auto_timer.isActive())
        self.window.auto_timer.timeout.emit()
        self.settled()
        self.assertEqual(self.market.calls[-1][2:], ('D1', True))

    def test_auto_does_not_queue_during_existing_request(self):
        self.window.load_moex()
        self.settled()
        self.market.calls.clear()
        self.market.gate = threading.Event()
        self.window.load_moex()
        self.window._auto_refresh()
        self.assertIsNone(self.window.pending_market)
        self.assertFalse(self.window.auto_timer.isActive())
        self.market.gate.set()
        self.settled()
        self.assertEqual(len(self.market.calls), 1)

    def test_auto_failure_keeps_chart_without_modal_dialog_and_schedules_retry(self):
        self.window.load_moex()
        self.settled()
        original = self.window.analysis
        self.market.error = 'offline'
        with patch('flowdesk.gui.QMessageBox.warning') as warning:
            self.window._auto_refresh()
            self.settled()
        warning.assert_not_called()
        self.assertIs(self.window.analysis, original)
        self.assertIn('offline', self.window.status.text())
        self.assertTrue(self.window.auto_timer.isActive())

    def test_csv_mode_stops_auto_refresh(self):
        self.window.load_moex()
        self.settled()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'days.csv'
            path.write_text('Time,Open,High,Low,Close\n2026-11-09,10,12,9,11\n2026-11-10,11,13,10,12\n2026-11-11,12,14,11,13\n')
            with patch('flowdesk.gui.QFileDialog.getOpenFileName', return_value=(str(path), 'CSV')):
                self.window.open_csv()
        count = len(self.market.calls)
        self.window._auto_refresh()
        self.assertEqual(len(self.market.calls), count)
        self.assertTrue(self.window.source.startswith('CSV:'))
        self.assertFalse(self.window.auto_timer.isActive())
        self.assertFalse(self.window.auto_checkbox.isEnabled())

    def test_auto_skips_unsubmitted_ticker(self):
        self.window.load_moex()
        self.settled()
        self.window.symbol.setText('AFKS')
        count = len(self.market.calls)
        self.window._auto_refresh()
        self.assertEqual(len(self.market.calls), count)
        self.assertEqual(self.window.loaded_market[0], 'SBER')

    def test_auto_completion_does_not_erase_newly_typed_ticker(self):
        self.window.load_moex()
        self.settled()
        self.market.calls.clear()
        self.market.gate = threading.Event()
        self.window._auto_refresh()
        self.window.symbol.setText('AFKS')
        self.market.gate.set()
        self.settled()
        self.assertEqual(self.window.loaded_market[0], 'SBER')
        self.assertEqual(self.window.symbol.text(), 'AFKS')


if __name__ == '__main__':
    unittest.main()

