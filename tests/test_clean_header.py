"""FlowDesk 0.2.26: compact header, unchanged instrument selection and engine."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from flowdesk import __version__
from flowdesk.gui import MainWindow


class CleanHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        window = MainWindow(startup=False, watchlist_service=False)
        window.resize(1420, 830)
        window.show()
        self.app.processEvents()
        self.addCleanup(window.close)
        return window

    def test_only_requested_controls_removed_from_screen(self):
        window = self.make_window()
        self.assertEqual(__version__, "0.2.26")
        # Legacy state is still available to the loader, but no widgets are
        # rendered in the toolbar or at arbitrary locations in the window.
        for widget in (window.board, window.symbol, window.csv_button, window.h1_button):
            with self.subTest(widget=type(widget).__name__):
                self.assertTrue(widget.isHidden())
                self.assertFalse(widget.isVisible())
        self.assertFalse(hasattr(window, "banner"))
        self.assertIsNotNone(window.tf)
        self.assertTrue(window.tf.isVisible())
        self.assertTrue(window.export_button.isVisible())
        self.assertTrue(window.settings_button.isVisible())
        self.assertTrue(window.instrument_list.isVisible())

    def test_watchlist_switches_hidden_symbol_board_for_futures(self):
        window = self.make_window()
        calls = []
        window._request_market = lambda *, refresh: calls.append(refresh)
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole,
                     {"symbol": "MXZ6", "board": "RFUD", "kind": "futures",
                      "name": "Index future"})
        window._instrument_clicked(item)
        self.assertEqual((window.symbol.text(), window.board.currentText()),
                         ("MXZ6", "RFUD"))
        self.assertEqual(calls, [False])
        self.assertTrue(window.symbol.isHidden())
        self.assertTrue(window.board.isHidden())
        self.assertEqual(window.tf.currentText(), "H1")

    def test_watchlist_switches_hidden_symbol_board_back_to_stock(self):
        window = self.make_window()
        calls = []
        window._request_market = lambda *, refresh: calls.append(refresh)
        for symbol, board in (("MXZ6", "RFUD"), ("SBER", "TQBR")):
            item = QTreeWidgetItem()
            item.setData(0, Qt.ItemDataRole.UserRole,
                         {"symbol": symbol, "board": board, "kind": "stock",
                          "name": symbol})
            window._instrument_clicked(item)
        self.assertEqual((window.symbol.text(), window.board.currentText()),
                         ("SBER", "TQBR"))
        self.assertEqual(calls, [False, False])


if __name__ == "__main__":
    unittest.main()
