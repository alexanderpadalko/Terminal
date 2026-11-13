"""Regression: failed TSLA selection must never leave the SBER chart on screen."""
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
from flowdesk.settings import SettingsStore
from test_gui_navigation import FakeMarket


class FinamMarket(FakeMarket):
    def __init__(self):
        super().__init__()
        self.release=threading.Event()
        self.block_symbol=None

    def view(self,ticker,mic,tf,**kwargs):
        if ticker==self.block_symbol:
            self.release.wait(4)
        if ticker=="TSLA":
            raise RuntimeError("TSLA@XNAS · H1: Finam HTTP 404")
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
        self.select("TSLA","XNAS")
        self.assertIsNone(self.window.chart.analysis)
        self.settle()
        self.assertIsNone(self.window.analysis)
        self.assertIsNone(self.window.chart.analysis)
        self.assertEqual(self.window.chart.parents,{})
        self.assertIn("TSLA@XNAS",self.window.chart.empty_text)
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
        self.market.block_symbol="TSLA"
        self.select("TSLA","XNAS")
        self.select("SBER")
        self.market.release.set()
        self.settle()
        self.assertIsNotNone(self.window.chart.analysis)
        self.assertIn("SBER@MISX",self.window.chart.instrument_header)
        self.assertNotIn("TSLA",self.window.status.text())

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
            self.assertEqual(other.symbol_edit.text(),"TSLA")
            self.assertEqual(other.mic.currentText(),"XNGS")
            self.assertEqual(other.ui_prefs.value("mic"),"XNGS")
        finally:
            other.close()
            other.deleteLater()
