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

    def test_archive_updates_wait_for_current_loader_and_coalesce(self):
        self.select('SBER');self.settle()
        self.window._history_changed('SBER@MISX')
        self.assertTrue(self.window.history_chart_timer.isActive())
        self.window.loader=object()
        try:
            with patch.object(self.window,'_load') as load:
                self.window._history_chart_refresh()
                load.assert_not_called()
        finally:self.window.loader=None
        with patch.object(self.window,'_load') as load:
            self.window._history_chart_refresh()
            self.window._history_chart_refresh()
            load.assert_called_once_with(refresh=False)
        self.window.history_chart_timer.stop()

    def test_archive_update_of_other_symbol_does_not_reload_selected_chart(self):
        self.select('SBER');self.settle()
        with patch.object(self.window,'_load') as load:
            self.window._history_changed('GAZP@MISX')
            self.window._history_chart_refresh()
            load.assert_not_called()

    def test_stale_failure_does_not_replace_newer_selection(self):
        self.market.block_symbol="GAZP"
        self.select("GAZP","MISX")
        self.select("SBER")
        self.market.release.set()
        self.settle()
        self.assertIsNotNone(self.window.chart.analysis)
        self.assertIn("SBER@MISX",self.window.chart.instrument_header)
        self.assertNotIn("GAZP",self.window.status.text())


    def test_fixed_universe_has_three_finam_groups_without_old_demo_assets(self):
        rows=[self.window.examples.item(i).text() for i in range(self.window.examples.count())]
        self.assertEqual(len(rows),4+10+8+3)
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

    def test_verified_us_ru_futures_update_before_moex_stocks(self):
        from flowdesk.finam_universe import (
            US_FUTURES, RU_FUTURES, RU_STOCKS, US_CONTRACTS, RU_CONTRACTS, Item)
        original_stocks=list(self.window.universe[RU_STOCKS])
        verified={
            US_FUTURES+":"+US_CONTRACTS[0].display:
                Item("GCZ2026","Золото",US_FUTURES,"GCZ6@XCEC",True,
                     "действует до 2026-12-18 · Finam GetAsset"),
            RU_FUTURES+":"+RU_CONTRACTS[0].display:
                Item("SIZ2026","Доллар/рубль",RU_FUTURES,"SiZ6@RTSX",True,
                     "действует до 2026-12-17 · Finam GetAsset")
        }
        self.window._futures_ready(verified)
        self.assertEqual(self.window.universe[US_FUTURES][0].symbol,"GCZ6@XCEC")
        self.assertEqual(self.window.universe[RU_FUTURES][0].symbol,"SiZ6@RTSX")
        self.assertEqual(self.window.universe[RU_STOCKS],original_stocks)
        self.assertTrue(self.window.daily_catalog_timer.isActive())

    def test_sidebar_starts_with_futures_then_top_ten_stocks(self):
        from flowdesk.finam_universe import US_FUTURES,RU_FUTURES,RU_STOCKS,GROUP_LABELS
        headings=[self.window.examples.item(i).text() for i in range(self.window.examples.count())
                  if self.window.examples.item(i).text() in GROUP_LABELS.values()]
        self.assertEqual(headings,[GROUP_LABELS[g] for g in (US_FUTURES,RU_FUTURES,RU_STOCKS)])
        self.assertEqual(self.window.examples.item(1).text().split(" · ")[0],"GCZ2026")

    def test_sidebar_has_no_text_search_but_keeps_tf_signal_filters(self):
        from PySide6.QtWidgets import QLineEdit
        self.assertFalse(hasattr(self.window,"search"))
        self.assertFalse(hasattr(self.window,"_filter_universe"))
        self.assertFalse(any("Поиск в выбранном списке" in field.placeholderText()
                             for field in self.window.findChildren(QLineEdit)))
        self.assertEqual(set(self.window.screen_buttons), {"H4","D","W","M"})
        self.assertEqual(self.window.examples.count(),25)
        self.window.screen_buttons["H4"].setChecked(True)
        self.assertLessEqual(self.window.examples.count(),26)
        self.window.screen_buttons["H4"].setChecked(False)
        self.assertEqual(self.window.examples.count(),25)

    def test_crypto_selection_cannot_start_a_network_connection(self):
        self.assertNotIn("BINANCE",[self.window.mic.itemText(i) for i in range(self.window.mic.count())])
        with patch.object(self.window,"_start_stream") as start:
            self.select("BTCUSDT","BINANCE")
        start.assert_not_called()
        self.assertIsNone(self.window.displayed_view)

    def test_catalog_outage_does_not_restore_retired_crypto_rows(self):
        self.window.catalog_error="Каталог Finam недоступен"
        self.window._populate_universe()
        rows=[self.window.examples.item(i).text() for i in range(self.window.examples.count())]
        self.assertFalse(any("BTC" in text or "КРИПТА" in text for text in rows))

    def test_manual_selection_cancels_deferred_saved_contract(self):
        self.window._pending_restore="TSLA@XNGS"
        with patch.object(self.window,"_load"), patch.object(self.window,"_start_stream"):
            self.select("SBER")
        self.assertIsNone(self.window._pending_restore)

    def test_incomplete_expiry_refresh_is_retried_even_with_healthy_catalog(self):
        from flowdesk.finam_universe import US_CONTRACTS,RU_CONTRACTS,RU_STOCKS,Item
        from flowdesk.watchlist import slot_date
        from datetime import datetime,timezone
        # Exact catalog symbols with unknown expiry remain selectable, but
        # they cannot mark the day's metadata check as completed.
        rows={c.group+":"+c.display:Item(c.display,c.name,c.group,
              c.aliases[0]+"@"+c.mics[0],True,
              "вечный контракт, без ролловера" if ".P" in c.display else
              "действует до 2027-12-18 · Finam GetAsset")
              for c in (*US_CONTRACTS,*RU_CONTRACTS)}
        key=US_CONTRACTS[0].group+":"+US_CONTRACTS[0].display
        good=rows[key]
        from dataclasses import replace
        rows[key]=replace(good,source="экспирация не подтверждена; автопереход приостановлен")
        payload={"assets":[],"finam_error":"","futures":rows,
                 "stocks":self.window.universe[RU_STOCKS],"ranking_status":"MOEX: подтверждено"}
        with patch.object(self.window,"_request_screener_scan"), \
             patch.object(self.window.futures_list_cache,"load",return_value={}), \
             patch.object(self.window,"_load"), patch.object(self.window,"_start_stream"):
            self.window._catalog_loaded(payload)
            self.assertIsNone(self.window._catalog_success_slot)
            rows[key]=good
            self.window._catalog_loaded(payload)
            self.assertEqual(self.window._catalog_success_slot,slot_date(datetime.now(timezone.utc)))

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
                labels=[widget.text() for widget in dialog.findChildren(QLabel)]
                self.assertFalse(any("Показывать боковую панель" in label or
                                     "Время свечи" in label or
                                     "готовые свечи Finam" in label
                                     for label in labels))
                dialog.refresh.setValue(120)
                _,edited=dialog.values()
                self.assertEqual(edited.refresh_seconds,120)
                self.assertEqual(edited.show_panel,original)
                self.assertTrue(edited.allow_http and edited.auto_updates)
                self.assertIn("Finam",dialog.token_button.text())
                dialog.restore_defaults()
                _,reset=dialog.values()
                self.assertEqual(reset.refresh_seconds,60)
                self.assertEqual(reset.show_panel,original)
            finally:
                dialog.close()
        # The working instrument list is unaffected by the removed checkbox.
        self.assertGreater(self.window.examples.count(),0)

    def test_screener_badges_have_one_visible_title_and_wider_sidebar(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import RU_STOCKS
        self.app.processEvents()
        pane=self.window.instrument_panel
        self.assertGreaterEqual(pane.minimumWidth(),360)
        self.assertGreater(pane.width(),290)
        self.assertEqual(pane.maximumWidth(),600)
        self.assertEqual(len(self.window.instrument_splitter.sizes()),2)
        entry=next(i for i in self.window.universe[RU_STOCKS]
                   if i.label=="SBER")
        self.window.screen_results[entry.symbol]=ScanResult(
            entry.symbol,(1,0,0,0))
        self.window.screen_buttons["H4"].setChecked(True)
        self.window._populate_universe()
        self.app.processEvents()
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith("SBER ·"))
        custom=self.window.examples.itemWidget(row)
        self.assertIsNotNone(custom)
        # The item keeps its real label for keyboard selection and accessibility,
        # but Qt must not paint that text underneath the custom badge row.
        self.assertEqual(row.foreground().color().alpha(),0)
        self.assertIn("SBER",row.text())
        labels=[label.text() for label in custom.findChildren(QLabel)]
        self.assertEqual(sum(label.startswith("SBER ·") for label in labels),1)
        self.assertIn("H4",labels)
        self.assertIn("H4",labels)
        self.assertEqual(row.data(Qt.ItemDataRole.UserRole),entry.symbol)
        # Selection used to override transparent foreground with Qt's
        # highlighted text palette and render the title a second time.
        from flowdesk.finam_gui import SignalRowDelegate
        self.assertIsInstance(self.window.examples.itemDelegate(),SignalRowDelegate)
        self.assertTrue(row.data(Qt.ItemDataRole.UserRole + 2))
        self.window.examples.setCurrentItem(row)
        self.app.processEvents()
        self.assertEqual(row.sizeHint().width(),0)
        self.assertEqual(sum(label.text().startswith("SBER ·")
            for label in custom.findChildren(QLabel)),1)
        self.assertEqual(self.window.examples.horizontalScrollBarPolicy(),
                         Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertLessEqual(custom.width(),self.window.examples.viewport().width())
        # Search has been removed; the marked FVG row remains visible.
        self.assertFalse(hasattr(self.window,"search"))
        self.assertFalse(row.isHidden())
        self.window.screen_buttons["H4"].setChecked(True)
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith("SBER ·"))
        self.assertEqual(row.foreground().color().alpha(),0)
        self.assertIsNotNone(self.window.examples.itemWidget(row))

    def test_unknown_badge_has_only_one_instrument_label(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel
        self.window.screen_buttons["D"].setChecked(True)
        self.app.processEvents()
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith("SBER ·"))
        widget=self.window.examples.itemWidget(row)
        self.assertIsNotNone(widget)
        self.assertEqual(row.foreground().color().alpha(),0)
        labels=[label.text() for label in widget.findChildren(QLabel)]
        self.assertEqual(sum(label.startswith("SBER ·") for label in labels),1)
        self.assertEqual(sum(text.startswith("D:") for text in labels),1)
        self.assertEqual(labels[-1],"D: ожидает проверки")

    def test_embedded_fvg_filter_or_unknown_section_and_chart_persist(self):
        from PySide6.QtWidgets import QLabel
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import RU_STOCKS
        by_label={i.label:i for i in self.window.universe[RU_STOCKS]}
        self.assertTrue(all(name in by_label for name in ("SBER","LKOH")))
        sber,lkoh=by_label["SBER"],by_label["LKOH"]
        self.assertEqual(tuple(self.window.screen_buttons),
                         ("H4","D","W","M"))
        self.assertFalse(any(b.isChecked() for b in
                             self.window.screen_buttons.values()))
        initial=[self.window.examples.item(i) for i in
                 range(self.window.examples.count())]
        self.assertEqual(len(initial),25)
        self.window.screen_results[sber.symbol]=ScanResult(
            sber.symbol,(1,0,0,0))
        self.window.screen_results[lkoh.symbol]=ScanResult(
            lkoh.symbol,(0,0,-1,0))
        self.window._populate_universe()
        self.select("LKOH")
        self.settle()
        analysis=self.window.chart.analysis
        self.assertIsNotNone(analysis)
        self.window.screen_buttons["H4"].setChecked(True)
        rows=[self.window.examples.item(i) for i in
              range(self.window.examples.count())]
        visible=[row for row in rows if not row.isHidden()]
        self.assertTrue(any("SBER · " in item.text() for item in visible))
        self.assertFalse(any("LKOH · " in item.text() for item in visible))
        self.assertTrue(any(item.text().startswith("НЕТ ДАННЫХ") for item in visible))
        self.assertIs(self.window.chart.analysis,analysis)
        self.assertEqual(self.window.current[0],"LKOH")
        sber_row=next(row for row in visible if row.text().startswith("SBER"))
        badges=[label.text() for label in
                self.window.examples.itemWidget(sber_row).findChildren(QLabel)]
        self.assertIn("H4",badges)
        self.assertIn("H4",badges) # All badges, not only selected TF.
        self.window.screen_buttons["W"].setChecked(True)
        rows=[self.window.examples.item(i) for i in
              range(self.window.examples.count())]
        self.assertTrue(any("LKOH · " in row.text() for row in rows
                            if not row.isHidden()))
        self.window.screen_buttons["H4"].setChecked(False)
        self.window.screen_buttons["W"].setChecked(False)
        self.assertEqual(self.window.examples.count(),25)
        self.assertIs(self.window.chart.analysis,analysis)

    def test_scanner_missing_history_stays_neutral_not_short(self):
        from PySide6.QtWidgets import QLabel
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import RU_STOCKS
        sber=next(i for i in self.window.universe[RU_STOCKS]
                  if i.label=="SBER")
        self.window.screen_results[sber.symbol]=ScanResult(
            sber.symbol,(None,None,None,None))
        self.window.screen_buttons["D"].setChecked(True)
        visible=[self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if not self.window.examples.item(i).isHidden()]
        sber_row=next(row for row in visible if row.text().startswith("SBER"))
        tags=[label.text() for label in
              self.window.examples.itemWidget(sber_row).findChildren(QLabel)]
        self.assertEqual(tags[-1],"D: ожидает проверки")
        self.assertNotIn("D",tags)

    def test_ru_si_eu_are_scheduled_before_other_scan_instruments(self):
        from unittest.mock import MagicMock
        self.mixed_case_currency_futures()
        self.window.catalog_ready=True
        with (patch("flowdesk.finam_gui.read_secret",return_value="local"),
              patch("flowdesk.finam_gui.FinamScreenerWorker") as worker_class):
            worker=MagicMock()
            worker_class.return_value=worker
            self.window._start_screener()
            instruments=worker_class.call_args.args[1]
            self.assertEqual([entry.label for entry in instruments[:2]],
                             ["SIZ2026","EUZ2026"])
            self.assertIn("SIZ6@RTSX",self.window.screener_pending)
            self.assertIn("EUZ6@RTSX",self.window.screener_pending)
            self.window.screener_worker=None

    def test_screener_accepts_normalized_si_eu_results_from_catalog_entries(self):
        from flowdesk.finam_screener import ScanResult
        entries=self.mixed_case_currency_futures()
        self.window.screen_buttons["H4"].setChecked(True)
        self.window.screener_pending={entry.symbol.upper() for entry in entries}
        for entry,direction in zip(entries,(1,-1)):
            result=ScanResult(entry.symbol.upper(),(direction,0,0,0))
            self.window._screener_row(result)
            with self.subTest(symbol=entry.symbol):
                self.assertIs(self.window.screen_results.get(result.symbol),result)
                self.assertNotIn(result.symbol,self.window.screener_pending)
                self.assertEqual(self.window._screen_outcome(entry,["H4"]),"matched")
        self.assertFalse(self.window.screener_pending)

    def test_unfinished_si_eu_get_explicit_retry_reason_after_scan(self):
        from unittest.mock import MagicMock
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel
        entries=self.mixed_case_currency_futures()
        self.window.screen_buttons["H4"].setChecked(True)
        worker=MagicMock()
        self.window.screener_worker=worker
        self.window.screener_pending={entry.symbol.upper() for entry in entries}
        with patch.object(self.window,"sender",return_value=worker):
            self.window._screener_finished()
        for entry in entries:
            with self.subTest(symbol=entry.symbol):
                result=self.window.screen_results.get(entry.symbol.upper())
                self.assertIsNotNone(result)
                self.assertEqual(result.error_code,"scan_incomplete")
                row=next(self.window.examples.item(i) for i in
                         range(self.window.examples.count()) if
                         self.window.examples.item(i).data(Qt.ItemDataRole.UserRole)==entry.symbol)
                labels=self.window.examples.itemWidget(row).findChildren(QLabel)
                self.assertTrue(any("проверка не завершена" in label.text()
                                    for label in labels))
                self.assertFalse(any("ожидает проверки" in label.text()
                                     for label in labels))
        self.assertFalse(self.window.screener_pending)
        self.assertIsNone(self.window.screener_worker)
        worker.deleteLater.assert_called_once()

    def test_no_filter_shows_all_30_instruments_without_any_tf_badges(self):
        from PySide6.QtWidgets import QLabel
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import RU_STOCKS
        sber=next(item for item in self.window.universe[RU_STOCKS]
                  if item.label=="SBER")
        self.window.screen_results[sber.symbol]=ScanResult(
            sber.symbol,(1,1,-1,-1))
        self.window._populate_universe()
        self.assertFalse(any(button.isChecked() for button in
                             self.window.screen_buttons.values()))
        self.assertEqual(self.window.examples.count(),25)
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith("SBER ·"))
        self.assertIsNone(self.window.examples.itemWidget(row))
        self.assertNotIn("нет данных",[self.window.examples.item(i).text()
                          for i in range(self.window.examples.count())])
        self.assertEqual(self.window.examples.objectName(),"instrumentList")

        self.assertIn("border:2px solid #687383",self.window.centralWidget().styleSheet())
        self.assertIn("border:1px solid #86baf0",self.window.centralWidget().styleSheet())
        self.window.screen_buttons["W"].setChecked(True)
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith("SBER ·"))
        widget=self.window.examples.itemWidget(row)
        self.assertIsNotNone(widget)
        badges=[label.text() for label in widget.findChildren(QLabel)]
        self.assertIn("H4",badges)
        self.assertIn("H4",badges)
        self.assertIn("M",badges)
        self.window.screen_buttons["W"].setChecked(False)
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith("SBER ·"))
        self.assertIsNone(self.window.examples.itemWidget(row))

    def test_unformed_selected_flow_is_excluded_without_unknown_row(self):
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import RU_STOCKS
        entry=next(row for row in self.window.universe[RU_STOCKS] if row.label=='SBER')
        self.window.screen_results[entry.symbol]=ScanResult(entry.symbol,(0,-1,0,None),
            missing_reasons=("","","","поток ещё не сформирован"))
        self.window.screen_buttons['M'].setChecked(True)
        self.assertEqual(self.window._screen_outcome(entry,('M',)),'excluded')
        self.assertFalse(any(self.window.examples.item(i).text().startswith('SBER ·')
                             for i in range(self.window.examples.count())))
        self.window.screen_buttons['D'].setChecked(True)
        rows=[self.window.examples.item(i) for i in range(self.window.examples.count())
              if self.window.examples.item(i).text().startswith('SBER ·')]
        self.assertEqual(len(rows),1)
        from PySide6.QtWidgets import QLabel
        labels=self.window.examples.itemWidget(rows[0]).findChildren(QLabel)
        self.assertIn('D',[label.text() for label in labels])
        self.assertFalse(any('сформирован' in label.text() for label in labels))
        self.assertFalse(any(label.objectName()=='screenerMissing' for label in labels))

    def test_missing_source_is_still_distinct_from_an_unformed_flow(self):
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import RU_STOCKS
        entry=self.window.universe[RU_STOCKS][0]
        self.window.screen_results[entry.symbol]=ScanResult(entry.symbol,(0,0,0,None),
            missing_reasons=("","","","нет свечей; нет доступа Finam (403)"))
        self.assertEqual(self.window._screen_outcome(entry,('M',)),'unknown')

    def test_unformed_tf_has_no_row_or_explanation_even_with_other_tf_signal(self):
        from PySide6.QtWidgets import QLabel
        from flowdesk.finam_screener import ScanResult
        from flowdesk.finam_universe import US_FUTURES, Item
        original=self.window.universe[US_FUTURES][0]
        entry=Item(original.label,original.name,original.group,
                   "GCZ26@XCEC",True,"Finam GetAsset")
        self.window.universe[US_FUTURES][0]=entry
        self.window.screen_results[entry.symbol]=ScanResult(
            entry.symbol,(1,1,None,None),
            missing_reasons=("","","поток ещё не сформирован","нет подтверждённого состояния"))
        self.window.screen_buttons["W"].setChecked(True)
        self.window.screen_buttons["M"].setChecked(True)
        self.assertFalse(any(self.window.examples.item(i).text().startswith(entry.label+" ·")
                             for i in range(self.window.examples.count())))
        self.window.screen_buttons["H4"].setChecked(True)
        row=next(self.window.examples.item(i) for i in
                 range(self.window.examples.count())
                 if self.window.examples.item(i).text().startswith(entry.label+" ·"))
        widget=self.window.examples.itemWidget(row)
        labels=widget.findChildren(QLabel)
        badge_labels=[label.text() for label in labels]
        self.assertIn("H4",badge_labels)
        self.assertIn("D",badge_labels)
        self.assertFalse(any(label.objectName()=="screenerMissing" for label in labels))
        self.assertFalse(any("сформирован" in label.text() or
                             "нет подтверждённого" in label.text() for label in labels))

    def test_known_future_expiry_is_visible_in_compact_sidebar(self):
        from flowdesk.finam_universe import Item, US_FUTURES
        entry=self.window.universe[US_FUTURES][0]
        self.window.universe[US_FUTURES][0]=Item(
            entry.label,entry.name,entry.group,"GCZ26@XCEC",True,
            "действует до 2026-12-18 · Finam GetAsset")
        self.window._populate_universe()
        labels=[self.window.examples.item(i).text()
                for i in range(self.window.examples.count())]
        self.assertTrue(any("GCZ2026" in label and "до 18.12" in label
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
        rows=[{"symbol":"GCZ6@XCEC","ticker":"GCZ6","mic":"XCEC",
               "type":"TYPE_FUTURE","is_archived":False}]
        self.window._catalog_loaded(rows)
        cells=[self.window.examples.item(i) for i in range(self.window.examples.count())]
        found=[row for row in cells if row.text().startswith("GCZ2026 ·")]
        self.assertEqual(len(found),1)
        self.assertEqual(found[0].data(Qt.ItemDataRole.UserRole),"GCZ6@XCEC")
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

    def test_old_h1_and_m1_preferences_migrate_to_h4(self):
        for retired in ('H1','M1'):
            self.window.ui_prefs.setValue('tf',retired)
            other=FinamWindow(service=self.market,settings_store=self.window.settings_store)
            try:
                self.assertEqual(other.tf.currentText(),'H4')
                self.assertEqual(tuple(other.tf.buttons),('M5','H4','D1','W1','MN1'))
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

    def test_catalog_can_update_finam_without_repeating_moex_ranking(self):
        from unittest.mock import MagicMock
        self.market.api=MagicMock()
        created=[]
        def make(*args,**kwargs):
            created.append(kwargs['refresh_ranking'])
            return MagicMock()
        with patch('flowdesk.finam_gui.read_secret',return_value='test'), \
             patch('flowdesk.finam_gui.FinamCatalogWorker',side_effect=make):
            self.window._refresh_catalog(force=True)
            self.window.catalog_worker=None
            self.window._refresh_catalog(force=True)
            self.window.catalog_worker=None
        self.assertEqual(created,[True,False])
        self.assertEqual(self.window.screener_timer.interval(),300000)

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

    def test_screener_requests_are_coalesced_to_five_minutes(self):
        from unittest.mock import MagicMock
        self.window.catalog_ready=True
        with patch('flowdesk.finam_gui.read_secret',return_value='test'), \
             patch('flowdesk.finam_gui.FinamScreenerWorker',return_value=MagicMock()) as make, \
             patch('flowdesk.finam_gui.monotonic',return_value=100) as clock:
            self.window._start_screener()
            self.assertEqual(make.call_count,1)
            self.window.screener_worker=None
            clock.return_value=200
            self.window._start_screener()
            self.assertEqual(make.call_count,1)
            self.assertGreaterEqual(self.window.screener_timer.interval(),200000)
            clock.return_value=401
            self.window._start_screener()
            self.assertEqual(make.call_count,2)
            self.assertEqual(self.window.screener_timer.interval(),300000)
            self.window.screener_worker=None
