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
        from flowdesk.finam_futures_cache import FuturesListCache
        class IsolatedFuturesListCache(FuturesListCache):
            def __init__(self):super().__init__(directory)
        self.cache_patch=patch('flowdesk.finam_gui.FuturesListCache',IsolatedFuturesListCache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
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

    def mixed_case_currency_futures(self):
        from dataclasses import replace
        from flowdesk.finam_universe import RU_FUTURES
        symbols={"SIZ2026":"SiZ6@RTSX","EUZ2026":"EuZ6@RTSX"}
        entries=[]
        for index,entry in enumerate(self.window.universe[RU_FUTURES]):
            if entry.label in symbols:
                entry=replace(entry,symbol=symbols[entry.label],verified=True)
                self.window.universe[RU_FUTURES][index]=entry
                entries.append(entry)
        return entries

    def test_corrupt_finam_store_reports_safe_error_not_qt_slot_exception(self):
        import sqlite3
        self.window.symbol_edit.setText("SBER")
        self.window.mic.setCurrentText("MISX")
        with patch("flowdesk.finam_gui.read_secret",return_value="test"), patch.object(
                self.window,"stream_factory",side_effect=sqlite3.DatabaseError("corrupt local store")):
            self.window._start_stream()
        self.assertIsNone(self.window.stream)
        self.assertIn("SQLite",self.window.status.text())
        self.assertIn("Finam",self.window.status.text())
        self.assertNotIn("corrupt local store",self.window.status.text())

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

    def test_switch_instrument_reframes_around_its_prices_instead_of_blank(self):
        from flowdesk.engine import analyze
        from flowdesk.market_data import Candle
        self.select("SBER")
        self.settle()
        self.window.chart.grab()
        chart=self.window.chart
        chart.price_range=(9.5,10.5)
        chart.viewport.count=45
        chart.viewport.offset=80
        self.assertIsNotNone(chart.analysis)
        original=self.market.view

        def differently_priced(ticker,mic,tf,**kwargs):
            analysis,parents,info=original(ticker,mic,tf,**kwargs)
            if ticker!="LKOH":
                return analysis,parents,info
            series=[Candle(c.time,c.open+6000,c.high+6000,
                           c.low+6000,c.close+6000,c.volume,
                           c.scheduled_close) for c in analysis.candles]
            return analyze(series,tf),parents,info

        with patch.object(self.market,"view",side_effect=differently_priced):
            self.select("LKOH")
            self.assertIsNone(chart.price_range)
            self.assertEqual(chart.viewport.offset,0)
            self.assertEqual(chart.viewport.count,125)
            self.settle()
            self.assertEqual(self.window.displayed_view,("LKOH","MISX","H4"))
            image=chart.grab().toImage()
            self.assertFalse(image.isNull())
            lower,upper=chart.last_price_range
            self.assertLess(lower,6009)
            self.assertGreater(upper,6011)
            self.assertIsNone(chart.price_range)
            self.assertGreater(chart.analysis.candles[-1].close,6000)

    def test_late_result_for_previous_timeframe_is_ignored(self):
        self.market.block_symbol="SBER"
        self.select("SBER")
        self.window.tf.setCurrentText("H4")
        old,parents,info=FakeMarket().view("SBER","MISX","M5",refresh=False)
        self.window._loaded(old,parents,info)
        self.assertIsNone(self.window.chart.analysis)
        self.market.release.set()
        self.settle()
        self.assertEqual(self.window.analysis.timeframe,"H4")
        self.assertEqual(self.window.displayed_view,("SBER","MISX","H4"))

    def test_rapid_return_cancels_old_chart_and_preserves_stream_and_http_updates(self):
        from dataclasses import replace
        from datetime import datetime, timezone
        from flowdesk.finam_stream import FinamEvent

        self.select("SBER")
        self.window.tf.setCurrentText("M5")
        self.settle()
        window = self.window
        old = FakeMarket().view("SBER", "MISX", "M5", refresh=False)
        first, last = old[0].candles[-2:]
        event = FinamEvent("candles", "SBER", "MISX", 1, datetime.now(timezone.utc),
                           timeframe="M5", candles=(first,))
        later = replace(event, candles=(last,))
        began = threading.Event()
        finish = threading.Event()
        calls = []
        interruption = []
        original = self.market.view

        def view(ticker, mic, tf, **kwargs):
            calls.append((tf, kwargs["refresh"]))
            if len(calls) == 1:
                worker = window.loader
                worker.progress.emit("stale progress")
                worker.failed.emit("stale failure")
                worker.loaded.emit(*old)
                began.set()
                finish.wait(8)
                interruption.append(kwargs["cancelled"]())
                if interruption[-1]:
                    raise InterruptedError()
            return original(ticker, mic, tf, **kwargs)

        with patch.object(self.market, "view", side_effect=view), \
             patch.object(self.market, "ingest_stream", create=True) as ingest:
            window.pending_bars[("M5", first.time)] = event
            window._flush()
            try:
                self.assertTrue(began.wait(3))
                worker = window.loader
                with patch("flowdesk.finam_gui.read_secret", return_value="test"):
                    window._http_refresh()
                    window._http_refresh()
                self.assertFalse(worker.superseded)
                window.tf.setCurrentText("D1")
                self.assertTrue(worker.isInterruptionRequested())
                window.pending_bars[("M5", last.time)] = later
                window.tf.setCurrentText("M5")
                self.assertIs(window.loader, worker)
                self.assertEqual(calls, [("M5", False)])

                # Signals from the old worker can already be waiting when
                # the selected ticker and timeframe match it again.
                status = window.status.text()
                self.app.processEvents()
                self.assertEqual(window.status.text(), status)
                self.assertIsNone(window.displayed_view)
            finally:
                finish.set()
                self.settle()
            self.assertEqual(interruption, [True])
            self.assertEqual(calls, [("M5", False), ("M5", True)])
            self.assertEqual([call.args for call in ingest.call_args_list], [
                ("SBER", "MISX", (event,)), ("SBER", "MISX", (later,)),
            ])
        self.assertEqual(window.displayed_view, ("SBER", "MISX", "M5"))
        self.assertFalse(window.pending_bars)
        self.assertFalse(window._http_refresh_pending)

    def test_new_chart_centers_last_candle_on_initial_display_and_tf_change(self):
        chart=self.window.chart
        def assert_last_centered():
            n=len(chart.analysis.candles)
            self.assertGreater(n, 0)
            left,_=chart.viewport.bounds(n)
            midpoint=(n-.5-left)/chart.viewport.span
            self.assertAlmostEqual(midpoint,.5,places=9)
            self.assertAlmostEqual(
                chart.viewport.right_padding,(chart.viewport.span-1)/2,places=9)
            self.assertEqual(chart.window()[1],n)
            self.assertEqual(chart.offset,0)
        self.select("SBER");self.settle()
        assert_last_centered()
        chart.viewport.count=47.5
        chart.viewport.offset=90.25
        chart.viewport.right_padding=5
        self.window.tf.setCurrentText("M5");self.settle()
        assert_last_centered()
        self.assertAlmostEqual(chart.viewport.count,47.5)
        chart.viewport.offset=50
        self.select("LKOH");self.settle()
        assert_last_centered()
        self.assertEqual(chart.viewport.count,125)

    def test_initial_latest_center_is_not_forced_on_same_chart_refresh(self):
        self.select("SBER");self.settle()
        chart=self.window.chart
        self.assertAlmostEqual(
            chart.viewport.right_padding,(chart.viewport.span-1)/2)
        chart.viewport.count=53.25
        chart.viewport.offset=67.5
        chart.viewport.right_padding=2.75
        chart.price_range=(9.2,13.7)
        manual=chart.view_state()
        self.window._load(refresh=True);self.settle()
        self.assertEqual(chart.view_state(),manual)

    def test_same_chart_refresh_preserves_fractional_pan_and_manual_price_scale(self):
        self.select("SBER");self.settle()
        chart=self.window.chart
        chart.viewport.count=63.5
        chart.viewport.offset=100.25
        chart.viewport.right_padding=3.75
        chart.price_range=(9.2,12.7)
        before=chart.view_state()
        self.window._load(refresh=True);self.settle()
        self.assertEqual(chart.view_state(),before)

    def test_reconnect_http_preserves_the_view_chosen_while_loading(self):
        from types import SimpleNamespace
        from datetime import datetime,timezone
        from flowdesk.finam_stream import FinamEvent
        self.select("SBER");self.settle()
        chart=self.window.chart
        self.market.block_symbol="SBER"
        self.market.release.clear()
        with patch("flowdesk.finam_gui.read_secret",return_value="mock"), \
             patch.object(self.window,"stream",SimpleNamespace(ticker="SBER",mic="MISX")):
            self.window._event(FinamEvent("status","SBER","MISX",2,
                datetime.now(timezone.utc),state="ready"))
            self.app.processEvents()
            self.assertIsNotNone(self.window.loader)
            chart.viewport.count=48.5
            chart.viewport.offset=115.75
            chart.price_range=(8,14)
            before=chart.view_state()
            self.market.release.set();self.settle()
            self.assertEqual(chart.view_state(),before)

    def test_same_monthly_chart_keeps_zoom_with_one_candle(self):
        from flowdesk.engine import analyze
        original=self.market.view
        def short_history(ticker,mic,tf,**kwargs):
            analysis,parents,info=original(ticker,mic,tf,**kwargs)
            return analyze(analysis.candles[-1:],tf),parents,info
        with patch.object(self.market,"view",side_effect=short_history):
            self.window.tf.setCurrentText("MN1");self.settle()
            self.select("SBER");self.settle()
            chart=self.window.chart
            chart.viewport.count=45.5
            chart.viewport.right_padding=23.25
            chart.price_range=(5,20)
            before=chart.view_state()
            self.window._load(refresh=True);self.settle()
            self.assertEqual(chart.view_state(),before)

    def test_updated_history_keeps_the_same_time_anchor(self):
        from flowdesk.engine import analyze
        from flowdesk.market_data import Candle
        from datetime import timedelta
        self.select("SBER");self.settle()
        chart=self.window.chart
        chart.viewport.count=50.5;chart.viewport.offset=100.25
        chart.viewport.right_padding=0
        anchor=chart.analysis.candles[chart.window()[1]-1].time
        bounds=chart.viewport.bounds(len(chart.times))
        original=self.market.view
        def added_bars(ticker,mic,tf,**kwargs):
            a,parents,info=original(ticker,mic,tf,**kwargs)
            first,last=a.candles[0],a.candles[-1]
            bars=[Candle(first.time-timedelta(hours=1),10,11,9,10),*a.candles,
                  Candle(last.time+timedelta(hours=1),10,11,9,10)]
            return analyze(bars,tf),parents,info
        with patch.object(self.market,"view",side_effect=added_bars):
            self.window._load(refresh=True);self.settle()
        self.assertEqual(chart.analysis.candles[chart.window()[1]-1].time,anchor)
        self.assertEqual(chart.viewport.bounds(len(chart.times)),(bounds[0]+1,bounds[1]+1))

    def test_stale_failure_does_not_replace_newer_selection(self):
        self.market.block_symbol="GAZP"
        self.select("GAZP","MISX")
        worker=self.window.loader
        self.select("SBER")
        self.assertTrue(worker.superseded)
        self.market.release.set()
        self.settle()
        self.assertIsNotNone(self.window.chart.analysis)
        self.assertIn("SBER@MISX",self.window.chart.instrument_header)
        self.assertNotIn("GAZP",self.window.status.text())


    def test_fixed_universe_has_three_finam_groups_without_old_demo_assets(self):
        rows=[self.window.examples.item(i).text() for i in range(self.window.examples.count())]
        self.assertEqual(len(rows),4+10+13+3)
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

    def test_fixed_watchlist_has_no_ranking_expiry_or_roll_jobs(self):
        from flowdesk.finam_universe import US_FUTURES,RU_FUTURES,RU_STOCKS
        self.assertFalse(hasattr(self.window,'daily_catalog_timer'))
        self.assertFalse(hasattr(self.window,'catalog_worker'))
        self.assertFalse(hasattr(self.window,'_refresh_catalog'))
        self.assertFalse(hasattr(self.window,'_catalog_loaded'))
        before={group:tuple(self.window.universe[group])
                for group in (US_FUTURES,RU_FUTURES,RU_STOCKS)}
        self.window._populate_universe()
        self.assertEqual(before,{group:tuple(self.window.universe[group])
                                 for group in before})
        self.assertEqual(self.window.examples.count(),30)
        self.assertTrue(all(not self.window.examples.item(i).text().count('до 18.12')
                            for i in range(self.window.examples.count())))

    def test_instrument_sidebar_has_no_screener_and_preserves_every_row(self):
        from PySide6.QtCore import Qt
        from flowdesk.finam_universe import US_FUTURES,RU_FUTURES,RU_STOCKS
        self.assertEqual(self.window.examples.count(),30)
        self.assertEqual([len(self.window.universe[g]) for g in
                          (US_FUTURES,RU_FUTURES,RU_STOCKS)],[4,13,10])
        for removed in ('screener_worker','screener_timer','screen_buttons',
                        'screen_results','scan_progress','history_worker',
                        '_request_screener_scan','_start_screener'):
            self.assertFalse(hasattr(self.window,removed),removed)
        for i in range(self.window.examples.count()):
            item=self.window.examples.item(i)
            self.assertFalse(item.isHidden())
            self.assertIsNone(self.window.examples.itemWidget(item))
            self.assertFalse(item.text().startswith('НЕТ ДАННЫХ'))
        self.assertEqual(self.window.examples.objectName(),'instrumentList')
        self.assertGreaterEqual(self.window.instrument_panel.minimumWidth(),360)
        sber=next(self.window.examples.item(i) for i in
                  range(self.window.examples.count()) if
                  self.window.examples.item(i).text().startswith('SBER · '))
        self.assertEqual(sber.data(Qt.ItemDataRole.UserRole),'SBER@MISX')
        self.window.examples.setCurrentItem(sber)
        self.window._populate_universe()
        self.assertEqual(self.window.examples.currentItem().data(
            Qt.ItemDataRole.UserRole),'SBER@MISX')
        self.assertEqual(self.window.examples.count(),30)

    def test_sidebar_starts_with_futures_then_top_ten_stocks(self):
        from flowdesk.finam_universe import US_FUTURES,RU_FUTURES,RU_STOCKS,GROUP_LABELS
        headings=[self.window.examples.item(i).text() for i in range(self.window.examples.count())
                  if self.window.examples.item(i).text() in GROUP_LABELS.values()]
        self.assertEqual(headings,[GROUP_LABELS[g] for g in (US_FUTURES,RU_FUTURES,RU_STOCKS)])
        self.assertEqual(self.window.examples.item(1).text().split(" · ")[0],"GCZ2026")

    def test_crypto_selection_cannot_start_a_network_connection(self):
        self.assertNotIn("BINANCE",[self.window.mic.itemText(i) for i in range(self.window.mic.count())])
        with patch.object(self.window,"_start_stream") as start:
            self.select("BTCUSDT","BINANCE")
        start.assert_not_called()
        self.assertIsNone(self.window.displayed_view)

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
        self.assertEqual(persisted.refresh_seconds,300)
        self.assertEqual(self.window.http_timer.interval(),300000)

    def test_finam_settings_hide_unused_panel_and_candle_explanation(self):
        from PySide6.QtWidgets import QCheckBox, QLabel
        from flowdesk.settings_dialog import SettingsDialog
        for original in (False, True):
            app=ApplicationSettings(False,90,original,False)
            dialog=SettingsDialog(self.window.indicator_settings,app,
                                  finam_only=True,token_action=lambda _:None)
            try:
                self.assertIsNone(dialog.panel)
                self.assertIsNone(dialog.auto)
                self.assertIsNone(dialog.http)
                self.assertIsNone(dialog.refresh)
                labels=[widget.text() for widget in dialog.findChildren(QLabel)]
                self.assertFalse(any("Показывать боковую панель" in label or
                                     "Время свечи" in label or
                                     "готовые свечи Finam" in label or
                                     "Интервал резервного" in label
                                     for label in labels))
                _,edited=dialog.values()
                self.assertEqual(edited.refresh_seconds,300)
                self.assertEqual(edited.show_panel,original)
                self.assertTrue(edited.allow_http and edited.auto_updates)
                self.assertIn("Finam",dialog.token_button.text())
                dialog.restore_defaults()
                _,reset=dialog.values()
                self.assertEqual(reset.refresh_seconds,300)
                self.assertEqual(reset.show_panel,original)
            finally:
                dialog.close()
        # The working instrument list is unaffected by the removed checkbox.
        self.assertGreater(self.window.examples.count(),0)

    def test_automatic_http_recheck_waits_for_busy_loader_and_coalesces_requests(self):
        from unittest.mock import MagicMock
        thread=MagicMock()
        self.window.loader=thread
        try:
            with patch('flowdesk.finam_gui.read_secret',return_value='test'), \
                 patch.object(self.window,'_load') as load:
                for _ in range(5):self.window._http_refresh()
                load.assert_not_called()
                self.assertTrue(self.window._http_refresh_pending)
                with patch.object(self.window,'sender',return_value=thread):
                    self.window._load_finished()
                load.assert_called_once_with(refresh=True,events=())
                self.assertFalse(self.window._http_refresh_pending)
        finally:
            self.window.loader=None

    def test_reconnect_ready_schedules_one_repair_and_ignores_duplicate_ready(self):
        from datetime import datetime,timezone
        from types import SimpleNamespace
        from flowdesk.finam_stream import FinamEvent
        self.window.stream=SimpleNamespace(ticker='SBER',mic='MISX')
        marked=[]
        self.market.mark_stream_reconnect=marked.append
        try:
            with patch.object(self.window,'_http_refresh') as refresh:
                self.window._event(FinamEvent('status','SBER','MISX',2,
                    datetime.now(timezone.utc),state='resync'))
                ready=FinamEvent('status','SBER','MISX',2,
                    datetime.now(timezone.utc),state='ready')
                self.window._event(ready)
                self.window._event(ready)
                self.app.processEvents()
                self.assertEqual(marked,['SBER@MISX'])
                refresh.assert_called_once_with()
        finally:
            self.window.stream=None

    def test_minimized_http_and_stream_repair_still_check_source(self):
        from datetime import datetime, timezone
        from flowdesk.finam_stream import FinamEvent

        self.select('SBER'); self.settle()
        window = self.window
        before = window.chart.analysis
        calls = len(self.market.calls)
        window.showMinimized()
        candle = before.candles[-1]
        event = FinamEvent('candles', 'SBER', 'MISX', 1, datetime.now(timezone.utc),
                           timeframe='H4', candles=(candle,))
        window.pending_bars[('H4', candle.time)] = event
        with patch.object(self.market, 'ingest_stream', create=True) as ingest, \
             patch.object(self.market, 'stream_repair_required', create=True, return_value=True):
            window._flush(); self.settle()
            ingest.assert_called_once_with('SBER', 'MISX', (event,))
        with patch('flowdesk.finam_gui.read_secret', return_value='test'):
            window._http_refresh(); self.settle()

        self.assertEqual(self.market.calls[calls:], [
            ('SBER', 'MISX', 'H4', False),
            ('SBER', 'MISX', 'H4', True),
        ])
        self.assertIs(window.chart.analysis, before)

    def test_unlisted_stock_cannot_replace_selected_chart(self):
        self.select("SBER")
        self.settle()
        chart=self.window.chart.analysis
        self.select("AAPL","XNGS")
        self.assertIs(self.window.chart.analysis,chart)
        self.assertIn("отсутствует",self.window.status.text())

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
            from flowdesk.finam_universe import RU_STOCKS
            fallback=other.universe[RU_STOCKS][0].symbol
            self.assertEqual(other.symbol_edit.text()+"@"+other.mic.currentText(),fallback)
            self.assertEqual(other.ui_prefs.value("mic"),"XNGS")
            self.assertIsNone(other._pending_restore)
        finally:
            other.close()
            other.deleteLater()

    def test_retired_hourly_and_m1_preferences_migrate_to_h4(self):
        for saved,expected in (('H1','H4'),('M1','H4')):
            self.window.ui_prefs.setValue('tf',saved)
            other=FinamWindow(service=self.market,settings_store=self.window.settings_store)
            try:
                self.assertEqual(other.tf.currentText(),expected)
                self.assertEqual(tuple(other.tf.buttons),
                                 ('M5','H4','D1','W1','MN1'))
            finally:
                other.close()
                other.deleteLater()

    def test_saved_crypto_preference_migrates_to_a_finam_stock(self):
        self.window.ui_prefs.setValue('ticker','BTCUSDT')
        self.window.ui_prefs.setValue('mic','BINANCE')
        other=FinamWindow(service=self.market,settings_store=self.window.settings_store)
        try:
            self.assertEqual(other.symbol_edit.text(),'SBER')
            self.assertEqual(other.mic.currentText(),'MISX')
            self.assertNotIn('Binance',other.windowTitle())
        finally:
            other.close();other.deleteLater()

    def test_switching_instruments_waits_until_the_old_socket_has_closed(self):
        from PySide6.QtCore import QObject,Signal
        class Stream(QObject):
            event=Signal(object)
            finished=Signal()
            def __init__(self,ticker,mic):
                super().__init__();self.ticker=ticker;self.mic=mic;self.stopped=False
            def start(self):pass
            def stop(self):self.stopped=True
        created=[]
        def make(ticker,mic):
            result=Stream(ticker,mic);created.append(result);return result
        with patch('flowdesk.finam_gui.read_secret',return_value='test'):
            self.window.stream_factory=make
            self.window.symbol_edit.setText('SBER');self.window.mic.setCurrentText('MISX')
            self.window._start_stream()
            old=created[0]
            self.window.symbol_edit.setText('LKOH')
            self.window._start_stream()
            self.assertTrue(old.stopped)
            self.assertEqual(len(created),1)
            self.assertIsNone(self.window.stream)
            old.finished.emit()
            self.assertEqual(len(created),2)
            self.assertEqual(self.window.stream.ticker,'LKOH')
            last=self.window.stream;self.window._stop_stream();last.finished.emit()
