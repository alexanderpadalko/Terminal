"""Regression: failed allowed-instrument selection must never leave the SBER chart on screen."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import concurrent.futures

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from flowdesk.finam_gui import FinamWindow
from flowdesk.runtime_probe import check_finam_runtime
from flowdesk.settings import SettingsStore, ApplicationSettings
from test_gui_navigation import FakeMarket


class FinamMarket(FakeMarket):
    def __init__(self):
        super().__init__()
        self.release=threading.Event()
        self.block_symbol=None

    def view(self,ticker,mic,tf,**kwargs):
        self.last_view_kwargs=dict(kwargs)
        if ticker==self.block_symbol:
            self.release.wait(4)
        if ticker=="GAZP":
            raise RuntimeError("GAZP@MISX · H1: Finam HTTP 404")
        return super().view(ticker,mic,tf,**kwargs)


class TestFinamGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory=Path(self.temp.name)
        self.key_patch=patch("flowdesk.finam_gui.read_secret",return_value="")
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.prefs_patch=patch("flowdesk.finam_gui.QSettings",
            side_effect=lambda *args:QSettings(str(directory/"prefs.ini"),QSettings.Format.IniFormat))
        self.prefs_patch.start()
        self.addCleanup(self.prefs_patch.stop)
        self.market=FinamMarket()
        self.window=FinamWindow(service=self.market,settings_store=SettingsStore(directory/"settings.json"))
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.cleanup_window)

    def settle(self):
        deadline=time.monotonic()+6
        while self.window.loader is not None and time.monotonic()<deadline:
            QTest.qWait(10)
        self.assertIsNone(self.window.loader)

    def cleanup_window(self):
        self.market.release.set()
        self.settle()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def select(self,ticker,mic="MISX"):
        self.window.symbol_edit.setText(ticker)
        self.window.mic.setCurrentText(mic)
        self.window._select()

    def test_failed_new_symbol_clears_previous_chart_and_labels_requested_symbol(self):
        self.select("SBER")
        self.settle()
        self.assertIsNotNone(self.window.chart.analysis)
        self.select("GAZP","MISX")
        self.assertIsNone(self.window.chart.analysis)
        self.settle()
        self.assertIsNone(self.window.analysis)
        self.assertIsNone(self.window.chart.analysis)
        self.assertEqual(self.window.chart.parents,{})
        self.assertIn("GAZP@MISX",self.window.chart.empty_text)
        self.assertIn("404",self.window.chart.empty_text)
        self.assertNotIn("SBER",self.window.feed.text())
        self.window.chart.grab()  # Render the empty/error surface too.

    def test_late_result_for_previous_timeframe_is_ignored(self):
        self.market.block_symbol="SBER"
        self.select("SBER")
        self.window.tf.setCurrentText("H4")
        old,parents,info=FakeMarket().view("SBER","MISX","H1",refresh=False)
        self.window._loaded(old,parents,info)
        self.assertIsNone(self.window.chart.analysis)
        self.market.release.set()
        self.settle()
        self.assertEqual(self.window.analysis.timeframe,"H4")
        self.assertEqual(self.window.displayed_view,("SBER","MISX","H4"))

    def test_stale_failure_does_not_replace_newer_selection(self):
        self.market.block_symbol="GAZP"
        self.select("GAZP","MISX")
        self.select("SBER")
        self.market.release.set()
        self.settle()
        self.assertIsNotNone(self.window.chart.analysis)
        self.assertIn("SBER@MISX",self.window.chart.instrument_header)
        self.assertNotIn("GAZP",self.window.status.text())


    def test_fixed_universe_has_only_three_groups_and_no_old_demo_assets(self):
        rows=[self.window.examples.item(i).text() for i in range(self.window.examples.count())]
        self.assertEqual(len(rows),8+20+10+3)
        self.assertFalse(self.window.symbol_edit.isVisible())
        self.assertFalse(self.window.mic.isVisible())
        self.assertFalse(any("AAPL" in s for s in rows))

    def test_diagnostic_controls_and_technical_rows_are_not_visible(self):
        from PySide6.QtWidgets import QPushButton
        self.assertFalse(self.window.feed.isVisible())
        self.assertFalse(self.window.universe_info.isVisible())
        buttons=[button.text() for button in
                 self.window.findChildren(QPushButton)]
        self.assertFalse(any("Диагностик" in text or
                             "Копировать отчёт" in text or
                             "Проверить повторно" in text for text in buttons))
        self.assertFalse(hasattr(self.window,"_show_expiry_report"))

    def test_finam_controls_are_absent_and_http_stream_are_on(self):
        from PySide6.QtWidgets import QPushButton, QCheckBox
        self.assertTrue(self.window.app_settings.auto_updates)
        self.assertTrue(self.window.app_settings.allow_http)
        self.assertFalse(hasattr(self.window,"auto"))
        self.assertFalse(hasattr(self.window,"http"))
        self.assertFalse(hasattr(self.window,"token_btn"))
        self.assertFalse(self.window.status.isVisible())
        forbidden=("Авто","HTTP","Последние · End","Автоцена · F","Токен Finam")
        visible=[w.text() for w in self.window.findChildren(QPushButton)
                 if w.isVisible()]
        self.assertFalse(any(word in visible for word in forbidden))
        self.assertFalse(any(widget.text() in ("Авто","HTTP")
                             for widget in self.window.findChildren(QCheckBox)
                             if widget.isVisible()))
        self.select("SBER")
        self.settle()
        self.assertTrue(self.market.last_view_kwargs["allow_http"])

    def test_settings_cannot_disable_finam_network_or_undo_http_enabled(self):
        from flowdesk.settings_dialog import SettingsDialog
        seen=[]
        dlg=SettingsDialog(self.window.indicator_settings,
                           ApplicationSettings(False,90,False,False),
                           finam_only=True,token_action=lambda parent:seen.append(parent))
        try:
            self.assertIsNone(dlg.auto)
            self.assertIsNone(dlg.http)
            self.assertIn("Finam",dlg.token_button.text())
            dlg.token_button.click()
            self.assertEqual(seen,[dlg])
            _,app=dlg.values()
            self.assertTrue(app.auto_updates and app.allow_http)
            dlg.restore_defaults()
            _,reset=dlg.values()
            self.assertTrue(reset.auto_updates and reset.allow_http)
        finally:
            dlg.close()
        with patch.object(self.window,"_load"),patch.object(self.window,"_start_stream"):
            self.window._apply_settings(
                self.window.indicator_settings,
                ApplicationSettings(False,120,False,False))
        persisted=self.window.settings_store.read()[1]
        self.assertTrue(persisted.allow_http and persisted.auto_updates)
        self.assertEqual(persisted.refresh_seconds,120)

    def test_known_future_expiry_is_visible_in_compact_sidebar(self):
        from flowdesk.finam_universe import Item, US_FUTURES
        entry=self.window.universe[US_FUTURES][0]
        self.window.universe[US_FUTURES][0]=Item(
            entry.label,entry.name,entry.group,"NQZ26@XCME",True,
            "действует до 2026-12-18 · Finam GetAsset")
        self.window._populate_universe()
        labels=[self.window.examples.item(i).text()
                for i in range(self.window.examples.count())]
        self.assertTrue(any("NQZ2026" in label and "до 18.12" in label
                            for label in labels))

    def test_unlisted_stock_cannot_replace_selected_chart(self):
        self.select("SBER")
        self.settle()
        chart=self.window.chart.analysis
        self.select("AAPL","XNGS")
        self.assertIs(self.window.chart.analysis,chart)
        self.assertIn("отсутствует",self.window.status.text())

    def test_catalog_enables_only_exact_matching_future(self):
        from PySide6.QtCore import Qt
        rows=[{"symbol":"NQZ6@XCME","ticker":"NQZ6","mic":"XCME",
               "type":"TYPE_FUTURE","is_archived":False}]
        self.window._catalog_loaded(rows)
        cells=[self.window.examples.item(i) for i in range(self.window.examples.count())]
        found=[row for row in cells if row.text().startswith("NQZ2026 ·")]
        self.assertEqual(len(found),1)
        self.assertEqual(found[0].data(Qt.ItemDataRole.UserRole),"NQZ6@XCME")
        blocked=[row for row in cells if row.text().startswith("BRNF2027 ·")]
        self.assertEqual(blocked[0].data(Qt.ItemDataRole.UserRole),"")

    def test_finam_worker_repairs_lazy_executor_before_to_thread_and_dns(self):
        output=Path(self.temp.name)/"runtime.json"
        with patch.object(concurrent.futures,"ThreadPoolExecutor",object()):
            with patch("socket.getaddrinfo",return_value=[("resolved",)]):
                self.assertEqual(check_finam_runtime(output),0)
        self.assertTrue(json.loads(output.read_text())["ok"])

    def test_old_saved_nasdaq_example_migrates_to_finam_segment_mic(self):
        self.window.ui_prefs.setValue("ticker","TSLA")
        self.window.ui_prefs.setValue("mic","XNAS")
        other=FinamWindow(service=self.market,settings_store=self.window.settings_store)
        try:
            self.assertEqual(other.symbol_edit.text(),"SBER")
            self.assertEqual(other.mic.currentText(),"MISX")
            self.assertEqual(other.ui_prefs.value("mic"),"XNGS")
            self.assertEqual(other._pending_restore,"TSLA@XNGS")
        finally:
            other.close()
            other.deleteLater()
