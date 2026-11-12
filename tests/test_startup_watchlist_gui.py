import os
import threading
import tempfile
from datetime import datetime
from pathlib import Path
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from flowdesk.gui import MainWindow
    from flowdesk.gui import demo_data
    from flowdesk.engine import analyze
    from test_gui_navigation import FakeMarket
    QT = True
except ImportError:
    if os.environ.get('CI') == 'true':
        raise
    QT = False

from flowdesk.preferences import Preferences
from flowdesk.market_data import MOSCOW
from flowdesk.watchlist import WatchlistService


@unittest.skipUnless(QT, 'Qt checked in Windows CI')
class TestStartupWatchlistGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def settle(self, w):
        end = time.monotonic() + 6
        self.app.processEvents()
        while w.worker is not None and time.monotonic() < end:
            QTest.qWait(10)
        self.app.processEvents()
        self.assertIsNone(w.worker)

    def test_startup_loads_last_instrument_without_button_and_saves_tf(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Preferences(Path(directory) / 'ui.json')
            store.write('AFKS', 'TQBR', 'M5')
            market = FakeMarket()
            w = MainWindow(service=market, startup=True, watchlist_service=False, preferences=store)
            w.show()
            self.settle(w)
            self.assertEqual(w.loaded_market, ('AFKS', 'TQBR', 'M5'))
            self.assertEqual(len(market.calls), 1)
            self.assertFalse(hasattr(w, 'load_button'))
            w.tf.setCurrentText('H1')
            self.settle(w)
            self.assertEqual(store.read()['tf'], 'H1')
            w.close()

    def test_footer_is_exchange_time_not_request_clock_and_hover_resets(self):
        w = MainWindow(service=FakeMarket(), watchlist_service=False)
        w.load_moex()
        self.settle(w)
        last = w.analysis.candles[-1].time.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M')
        self.assertIn(last, w.candle_time.text())
        self.assertIn('Получено', w.data_age.text())
        w._hover(1)
        self.assertIn(w.analysis.candles[1].time.strftime('%d.%m.%Y %H:%M'), w.candle_time.text())
        w._hover(-1)
        self.assertIn(last, w.candle_time.text())
        w.close()

    def test_sidebar_50_symbols_search_and_click_loads_correct_board(self):
        market = FakeMarket()
        w = MainWindow(service=market, watchlist_service=False)
        self.assertEqual(len(w.list_items), 50)
        self.assertFalse(w.watchlist_panel.isHidden())
        item = w.list_items[('AFKS', 'TQBR')]
        w.list_search.setText('AFKS')
        self.assertFalse(item.isHidden())
        self.assertTrue(w.list_items[('SBER', 'TQBR')].isHidden())
        w._instrument_clicked(item)
        self.settle(w)
        self.assertEqual(w.loaded_market[:2], ('AFKS', 'TQBR'))
        w._instrument_clicked(w.list_items[('SIZ6', 'RFUD')])
        self.settle(w)
        self.assertEqual(w.loaded_market[:2], ('SIZ6', 'RFUD'))
        w.close()

    def test_close_before_queued_start_prevents_late_load(self):
        market = FakeMarket()
        w = MainWindow(service=market, startup=True, watchlist_service=False)
        w.close()
        QTest.qWait(20)
        self.app.processEvents()
        self.assertEqual(market.calls, [])

    def test_late_startup_retry_does_not_replace_a_csv_opened_by_user(self):
        market = FakeMarket()
        market.error = 'offline'
        w = MainWindow(service=market, startup=True, watchlist_service=False)
        self.settle(w)
        csv = analyze(demo_data(), 'H1')
        w._show_data(csv, {}, 'CSV: chosen.csv')
        market.error = None
        count = len(market.calls)
        w._startup_load()
        self.settle(w)
        self.assertEqual(len(market.calls), count)
        self.assertIs(w.analysis, csv)
        w.close()

    def test_close_cancels_inflight_list_worker_without_destroying_qthread(self):
        cancelled = threading.Event()
        class SlowList:
            def cached(self):
                return WatchlistService().cached()
            def due(self, value):
                return True
            def refresh(self, stop):
                deadline = time.monotonic() + 4
                while not stop() and time.monotonic() < deadline:
                    time.sleep(.005)
                if stop():
                    cancelled.set()
                    raise InterruptedError('stopped')
                return self.cached()
        w = MainWindow(service=FakeMarket(), watchlist_service=SlowList())
        w.show()
        QTest.qWait(30)
        self.assertIsNotNone(w.list_worker)
        w.close()
        deadline = time.monotonic() + 4
        while w.list_worker is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(w.list_worker)
        self.assertFalse(w.isVisible())
