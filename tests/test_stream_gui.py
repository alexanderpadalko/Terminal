import os
import time
import unittest
from datetime import datetime

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import MainWindow
    from test_gui_navigation import FakeMarket
    QT_AVAILABLE = True
except ImportError:
    if os.environ.get('CI') == 'true':
        raise
    QT_AVAILABLE = False

from flowdesk.iss_plus import FeedEvent
from flowdesk.market_data import Candle, MOSCOW

NOW = datetime(2026, 11, 11, 13, 30, tzinfo=MOSCOW)

if QT_AVAILABLE:
    class FakeStream(QObject):
        event = Signal(object)
        finished = Signal()

        def __init__(self, symbol, board):
            super().__init__()
            self.symbol, self.board, self.stopped = symbol, board, False

        def start(self):
            pass

        def stop(self):
            self.stopped = True
            self.finished.emit()

        def send(self, kind='status', state='', **kwargs):
            self.event.emit(FeedEvent(kind, self.symbol, self.board, 1, NOW, state=state, **kwargs))

    class StreamMarket(FakeMarket):
        def __init__(self):
            super().__init__()
            self.batches, self.repairs = [], 0

        def view(self, *args, **kwargs):
            result = super().view(*args, **kwargs)
            if kwargs.get('with_minutes'):
                self.repairs += 1
                result[2]['tier'] = 'network'
            if self.batches and self.batches[-1][:2] == args[:2]:
                result[2]['stream_received_at'] = NOW.isoformat()
            return result

        def ingest_stream(self, symbol, board, batch):
            self.batches.append((symbol, board, batch))


@unittest.skipUnless(QT_AVAILABLE, 'Qt runs in the pinned Windows runtime')
class TestStreamGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.streams = []
        def factory(*args):
            stream = FakeStream(*args)
            self.streams.append(stream)
            return stream
        self.market = StreamMarket()
        self.window = MainWindow(service=self.market, stream_factory=factory)
        self.window.http_checkbox.setChecked(True)
        self.window.show()
        self.window.load_moex()
        self.settle()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.settle()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def settle(self):
        limit = time.monotonic() + 5
        while self.window.worker is not None and time.monotonic() < limit:
            QTest.qWait(10)
        self.app.processEvents()
        self.assertIsNone(self.window.worker)

    def ready(self):
        stream = self.streams[-1]
        stream.send(state='resync')
        stream.send(state='ready')
        self.window._flush_stream()
        self.settle()
        self.assertFalse(self.window.stream_needs_repair)
        return stream

    def test_stream_requires_repair_then_no_healthy_http_poll(self):
        self.ready()
        self.assertEqual(self.market.repairs, 1)
        count = len(self.market.calls)
        self.window._auto_refresh()
        self.assertIsNone(self.window.worker)
        self.assertEqual(len(self.market.calls), count)

    def test_quiet_session_boundary_triggers_one_fresh_http_repair(self):
        self.ready()
        count = self.market.repairs
        self.window.data_info['next_confirmation_at'] = '2020-01-01T00:00:00+03:00'
        self.window._auto_refresh()
        self.settle()
        self.assertEqual(self.market.repairs, count + 1)
        self.assertFalse(self.window.stream_needs_repair)

    def test_stream_batches_do_not_postpone_boundary_timer(self):
        stream = self.ready()
        self.window.auto_timer.setInterval(5000)
        self.window.auto_timer.start()
        QTest.qWait(30)
        remaining = self.window.auto_timer.remainingTime()
        stream.send(kind='candles', timeframe='M1', candles=(Candle(NOW, 10, 12, 9, 11),))
        self.window._flush_stream()
        self.settle()
        self.assertTrue(self.window.auto_timer.isActive())
        self.assertLess(self.window.auto_timer.remainingTime(), remaining)

    def test_quote_does_not_replace_candle_or_view(self):
        stream = self.ready()
        old = self.window.analysis
        self.window.chart.price_range = (4, 20)
        stream.send(kind='quote', quote={'LAST': 1234, 'TIME': '13:15:00', 'TRADINGSTATUS': 'T'})
        QTest.qWait(280)
        self.assertIs(self.window.analysis, old)
        self.assertEqual(self.window.chart.price_range, (4, 20))
        self.assertIn('1234', self.window.feed_label.text())

    def test_badge_stays_connected_while_candle_packet_is_processed(self):
        stream=self.ready()
        caption=self.window.source_indicator.text()
        stream.send(kind='candles',timeframe='M1',candles=(Candle(NOW,10,12,9,11),))
        self.window._flush_stream()
        self.assertEqual(self.window.source_indicator.text(),caption)
        self.settle()
        self.assertEqual(self.window.source_indicator.text(),caption)

    def test_quote_burst_is_coalesced_without_starting_candle_workers(self):
        from unittest.mock import patch
        stream=self.ready()
        with patch.object(self.window,'_update_channel_indicator',wraps=self.window._update_channel_indicator) as update:
            for i in range(100):stream.send(kind='quote',quote={'LAST':100+i,'TIME':'13:15:00'})
            self.assertEqual(update.call_count,0)
            # Windows CI can delay a 250ms Qt timer under heavy test load.
            deadline=time.monotonic()+2
            while update.call_count == 0 and time.monotonic() < deadline:
                QTest.qWait(30)
            self.assertEqual(update.call_count,1)
        self.assertIn('199',self.window.feed_label.text())

    def test_tf_switch_keeps_connection_and_stream_batch_uses_worker(self):
        stream = self.ready()
        self.window.tf.setCurrentText('M1')
        self.settle()
        self.assertEqual(len(self.streams), 1)
        stream.send(kind='candles', timeframe='M1', candles=(Candle(NOW, 10, 12, 9, 11),))
        self.window._flush_stream()
        self.settle()
        self.assertEqual(len(self.market.batches), 1)
        self.assertEqual(self.window.data_info['tier'], 'stream')

    def test_instrument_switch_stops_old_and_ignores_old_events(self):
        old = self.ready()
        self.window.symbol.setText('AFKS')
        self.window.load_moex()
        self.settle()
        self.assertTrue(old.stopped)
        # QObject may have been deleted by the event loop; deliver a stale
        # event using the new sender but old identity to test both guards.
        new = self.streams[-1]
        new.event.emit(FeedEvent('quote', 'SBER', 'TQBR', 1, NOW, quote={'LAST': 999}))
        self.assertNotIn('999', self.window.feed_label.text())
        self.assertEqual(self.window.stream_key, ('AFKS', 'TQBR'))

    def test_pause_stops_and_resume_requires_new_repair(self):
        old = self.ready()
        self.window.auto_checkbox.setChecked(False)
        self.assertTrue(old.stopped)
        self.assertFalse(self.window.auto_timer.isActive())
        self.window.auto_checkbox.setChecked(True)
        self.assertEqual(len(self.streams), 2)
        self.assertTrue(self.window.stream_needs_repair)

    def test_permission_failure_uses_http_fallback(self):
        stream = self.ready()
        stream.send(state='unavailable', detail='not authorized')
        stream.finished.emit()
        self.window._auto_refresh()
        self.settle()
        self.assertEqual(len(self.streams), 1)
        self.assertIsNone(self.window.stream_worker)
        self.assertTrue(self.window.auto_timer.isActive())

    def test_offline_repair_does_not_create_a_tight_http_loop(self):
        self.ready()
        original = self.market.view
        def offline(*args, **kwargs):
            a, parents, info = original(*args, **kwargs)
            info.update(tier='offline', stream_repair_required=True)
            return a, parents, info
        self.market.view = offline
        self.window.stream_needs_repair = True
        self.window._auto_refresh()
        self.settle()
        self.assertTrue(self.window.stream_needs_repair)
        self.assertFalse(self.window.stream_repair_pending)
        self.assertIsNone(self.window.worker)
        self.assertTrue(self.window.auto_timer.isActive())

    def test_channel_indicator_tracks_websocket_repair_and_fallback(self):
        indicator = self.window.source_indicator
        self.assertEqual(indicator.property('channel'), 'pending')
        self.assertIn('ПОДКЛ', indicator.text())
        stream = self.streams[-1]
        stream.send(state='resync')
        self.assertEqual(indicator.property('channel'), 'http')
        self.assertIn('СВЕРКА', indicator.text())
        stream.send(state='ready')
        self.window._flush_stream()
        self.settle()
        self.assertEqual(indicator.property('channel'), 'stream')
        self.assertIn('ISS+', indicator.text())
        stream.send(state='reconnecting')
        self.assertEqual(indicator.property('channel'), 'http')
        self.assertIn('РЕЗЕРВ', indicator.text())
        stream.send(state='unavailable')
        stream.finished.emit()
        self.assertEqual(indicator.property('channel'), 'http')

    def test_disabled_http_blocks_repair_and_stream_still_updates(self):
        stream = self.streams[-1]
        self.window.http_checkbox.setChecked(False)
        self.assertFalse(self.window.application_settings.allow_http)
        self.assertFalse(self.window.auto_timer.isActive())
        stream.send(state='resync')
        stream.send(state='ready')
        self.window._flush_stream()
        self.settle()
        self.assertEqual(self.market.repairs, 0)
        stream.send(kind='candles', timeframe='M1',
                    candles=(Candle(NOW, 10, 12, 9, 11),))
        self.window._flush_stream()
        self.settle()
        self.assertEqual(len(self.market.batches), 1)
        self.assertEqual(self.window.data_info['tier'], 'stream')
        stream.send(state='reconnecting')
        self.assertNotEqual(self.window.source_indicator.text(), 'HTTP · РЕЗЕРВ')

    def test_rfud_stream_with_http_disabled_keeps_stream_badge_and_updates(self):
        old = self.ready()
        self.window.http_checkbox.setChecked(False)
        self.window.board.setCurrentText('RFUD')
        self.window.symbol.setText('MXZ6')
        self.window.load_moex()
        self.settle()
        self.assertTrue(old.stopped)
        stream = self.streams[-1]
        self.assertEqual((stream.symbol, stream.board), ('MXZ6', 'RFUD'))
        count = self.market.repairs
        stream.send(state='resync')
        stream.send(state='ready')
        stream.send(kind='candles', timeframe='M1', candles=(Candle(NOW, 10, 12, 9, 11),))
        self.window._flush_stream()
        self.settle()
        self.assertEqual(self.market.repairs, count)
        self.assertEqual(self.market.batches[-1][:2], ('MXZ6', 'RFUD'))
        self.assertEqual(self.window.source_indicator.text(), 'ПОТОК · ISS+')
        self.assertFalse(self.window.http_checkbox.isChecked())
        self.window.tf.setCurrentText('H1')
        self.settle()
        self.assertIs(self.window.stream_worker, stream)
        stream.send(state='unavailable', detail='denied')
        stream.finished.emit()
        self.window._auto_refresh()
        self.settle()
        self.assertEqual(self.market.repairs, count)
        self.assertEqual(self.window.source_indicator.text(), 'ПОТОК · НЕДОСТУПЕН')

    def test_channel_indicator_shows_paused_auto_updates(self):
        self.ready()
        self.window.auto_checkbox.setChecked(False)
        self.assertEqual(self.window.source_indicator.property('channel'), 'idle')
        self.assertIn('ПАУЗА', self.window.source_indicator.text())
