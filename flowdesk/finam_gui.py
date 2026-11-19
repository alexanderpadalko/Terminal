"""FlowDesk: Finam market data only, no order APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from functools import partial
from contextlib import nullcontext
from pathlib import Path
import sqlite3
import re
import sys

from PySide6.QtCore import QEvent, QSettings, QThread, QTimer, Qt, Signal, QSize
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
    QListWidget, QListWidgetItem, QInputDialog, QSplitter, QFormLayout, QFrame,
    QSizePolicy
)

from . import __version__
from .app_icon import application_icon, set_windows_application_id
from .finam_api import read_secret, save_secret, delete_secret, parse_instant
from .market_symbols import symbol_name,BINANCE
from .market_services import MarketServices
from .exchange_time import exchange_clock
from .finam_universe import (
    US_FUTURES, RU_STOCKS, RU_FUTURES, GROUP_LABELS,
    US_CONTRACTS, RU_CONTRACTS,
    compose_universe, cached_top10, is_in_universe
)
from .finam_catalog_worker import FinamCatalogWorker
from .finam_futures_cache import FuturesListCache
from .watchlist import slot_date
from .qt_gc import install_gui_gc
from .diagnostics_log import log_event, log_snapshot
from .finam_stream import FinamStreamWorker
from .gui import CandleChart, TimeframeBar
from .terminal_policy import DISPLAY_TIMEFRAMES, HTTP_REFRESH_SECONDS
from .settings import IndicatorSettings, ApplicationSettings, SettingsStore
from .settings_dialog import SettingsDialog

EXAMPLES = (
    ("SBER","MISX","Сбербанк · Москва"),
    ("GAZP","MISX","Газпром · Москва"),
    ("AAPL","XNGS","Apple · NASDAQ"),
    ("MSFT","XNGS","Microsoft · NASDAQ"),
    ("NVDA","XNGS","Nvidia · NASDAQ"),
    ("TSLA","XNGS","Tesla · NASDAQ"),
    ("IBM","XNYS","IBM · NYSE"),
)


class FinamLoadWorker(QThread):
    loaded = Signal(object,object,object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, service, ticker, mic, timeframe, settings,
                 *, allow_http=False, refresh=False, events=(), defer_chart=False):
        super().__init__()
        self.service, self.ticker, self.mic = service, ticker, mic
        self.timeframe, self.settings = timeframe, settings
        self.allow_http, self.refresh, self.events = allow_http, refresh, events
        self.defer_chart = defer_chart
        self.superseded = False

    def run(self):
        scope=getattr(getattr(self.service,"api",None),"request_scope",None)
        with scope(priority=0,cancelled=self.isInterruptionRequested) if scope else nullcontext():
            try:
                self._run()
            finally:
                if self.defer_chart:
                    release = getattr(self.service, 'release_chart_sources', None)
                    if callable(release):
                        release()

    def _run(self):
        try:
            if self.events:
                self.service.ingest_stream(self.ticker,self.mic,self.events)
            if self.isInterruptionRequested():
                return
            if self.defer_chart and self.events and not self.refresh:
                requires_repair = getattr(self.service, 'stream_repair_required', None)
                if callable(requires_repair) and not requires_repair(symbol_name(self.ticker, self.mic)):
                    return  # The stream batch is stored; chart analysis can wait.
            getter=getattr(getattr(self.service,'api',None),'asset_tick_size',None)
            step=None
            if callable(getter):
                try:step=getter(symbol_name(self.ticker,self.mic))
                except Exception:pass
            result = self.service.view(self.ticker,self.mic,self.timeframe,
                allow_http=self.allow_http,refresh=self.refresh,
                settings=self.settings,progress=self.progress.emit,
                cancelled=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                return
            # Asset metadata is read-only and independent of the HTTP *candle*
            # checkbox. Resolve its true tick size in the worker, never on Qt.
            if step is not None:
                analysis,parents,info=result
                result=(analysis,parents,{**info,"price_tick_size":step})
            if not self.isInterruptionRequested():
                self.loaded.emit(*result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(f"{type(exc).__name__}: {str(exc)[:260]}")


class FinamWindow(QMainWindow):
    def __init__(self, *, service=None, stream_factory=None, settings_store=None):
        super().__init__()
        install_gui_gc(QApplication.instance())
        self.setWindowTitle(f"FlowDesk {__version__} · Finam Trade API · только данные")
        self.setWindowIcon(application_icon())
        self.resize(1420,840)
        self.setMinimumSize(1040,650)
        self.service = service or MarketServices()
        self.stream_factory = stream_factory or self._make_stream
        self.settings_store = settings_store or SettingsStore()
        self.indicator_settings, self.app_settings = self.settings_store.read()
        # Finam-only terminal has unconditional WebSocket and HTTP candle access.
        # Old saved settings must never silently turn either one off.
        self.app_settings=replace(self.app_settings,auto_updates=True,allow_http=True,
                                  refresh_seconds=HTTP_REFRESH_SECONDS)
        self.ui_prefs = QSettings("FlowDesk","FinamOnly")
        self.loader = None
        self.stream = None
        self.stream_ready = False
        self.stream_state = "Не подключён"
        self.stream_error = ""
        self.current = None
        self.current_view = None
        self.displayed_view = None
        self.pending_load = None
        self.pending_bars = {}
        self._http_refresh_pending = False
        self._restore_refresh_pending = False
        self.closing = False
        self.analysis = None
        self.parents = {}
        self.catalog_worker = None
        self.catalog_ready = False
        self.catalog_error = ""
        self.futures_list_cache = FuturesListCache()
        self._catalog_success_slot = None
        self._catalog_attempt_at = None
        self._ranking_attempted=False
        self._pending_stream=False
        self.universe, self.ranking_status = compose_universe()
        cached_futures = self.futures_list_cache.load()
        for group in (US_FUTURES, RU_FUTURES):
            self.universe[group] = [
                cached_futures.get(group+":"+row.label, row)
                for row in self.universe[group]]
        self._make_ui()
        self.feed_timer = QTimer(self)
        self.feed_timer.setSingleShot(True)
        self.feed_timer.setInterval(1600)
        self.feed_timer.timeout.connect(self._flush)
        self.http_timer = QTimer(self)
        self.http_timer.setInterval(self.app_settings.refresh_seconds*1000)
        self.http_timer.timeout.connect(self._http_refresh)
        self.daily_catalog_timer = QTimer(self)
        self.daily_catalog_timer.setInterval(60000)
        self.daily_catalog_timer.timeout.connect(self._daily_catalog_refresh)
        self.daily_catalog_timer.start()
        # Hourly no-token checks trigger at most one backup per UTC day,
        # without blocking Qt rendering or network worker priorities.
        self.storage_backup_timer=QTimer(self)
        self.storage_backup_timer.setInterval(3600000)
        self.storage_backup_timer.timeout.connect(self._backup_stores)
        self.storage_backup_timer.start()
        QTimer.singleShot(1500,self._backup_stores)
        ticker=str(self.ui_prefs.value("ticker","SBER"))
        mic=str(self.ui_prefs.value("mic","MISX"))
        if mic==BINANCE:
            ticker,mic='SBER','MISX'
            self.ui_prefs.setValue('ticker',ticker)
            self.ui_prefs.setValue('mic',mic)
        self._pending_restore=ticker.upper()+"@"+mic.upper()
        # Migrate only the four previously incorrect built-in examples. Never
        # guess a different venue for arbitrary user-entered instruments.
        if mic=="XNAS" and ticker in {t for t,m,_ in EXAMPLES if m=="XNGS"}:
            mic="XNGS"
            self.ui_prefs.setValue("mic",mic)
        self._pending_restore=ticker.upper()+"@"+mic.upper()
        self.symbol_edit.setText(ticker)
        self.mic.setCurrentText(mic)
        if is_in_universe(self._pending_restore,self.universe):
            self._pending_restore=None
        else:
            # Futures are resolved asynchronously; do not open an old AAPL
            # selection or guess the target MIC while awaiting the directory.
            self.symbol_edit.setText("SBER")
            self.mic.setCurrentText("MISX")
        tf = str(self.ui_prefs.value("tf","H4"))
        blocked=self.tf.blockSignals(True)
        self.tf.setCurrentText(tf if tf in self.tf.buttons else "H4")
        self.tf.blockSignals(blocked)
        self._indicator()
        QTimer.singleShot(0,self._startup)

    def _make_ui(self):
        root=QWidget(self)
        self.setCentralWidget(root)
        outer=QVBoxLayout(root)
        outer.setContentsMargins(10,10,10,10)
        outer.setSpacing(9)
        header=QHBoxLayout()
        emblem=QLabel()
        emblem.setPixmap(application_icon().pixmap(30,30))
        header.addWidget(emblem)
        wordmark=QLabel("FLOWDESK")
        wordmark.setStyleSheet("font-size: 19px; font-weight: 800; letter-spacing: 1px")
        header.addWidget(wordmark)
        header.addSpacing(20)
        self.tf=TimeframeBar(frames=DISPLAY_TIMEFRAMES)
        self.tf.currentTextChanged.connect(lambda _:self._load(refresh=False))
        header.addWidget(self.tf)
        header.addStretch()
        self.channel=QLabel()
        self.channel.setMinimumWidth(190)
        self.channel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.channel)
        self.settings_btn=QPushButton("⚙")
        self.settings_btn.setToolTip("Настройки FlowDesk · индикатор и токен Finam")
        self.settings_btn.clicked.connect(self._settings)
        header.addWidget(self.settings_btn)
        outer.addLayout(header)
        self.feed=QLabel("Finam: ожидаем данные")
        self.feed.setStyleSheet("color:#a4a4a4")
        outer.addWidget(self.feed)
        self.feed.hide()  # Technical observation belongs in diagnostics.log.
        split=QSplitter(Qt.Orientation.Horizontal)
        self.chart=CandleChart(terminal=True)
        self.chart.set_settings(self.indicator_settings)
        self.chart.set_chart_colors(self.app_settings)
        self.chart.empty_text="Добавьте токен Finam в настройках и выберите инструмент"
        split.addWidget(self.chart)
        side=QFrame()
        # Make room for the instrument name and five independent TF badges.
        # The splitter stays resizable; 460 px is only the initial width.
        side.setMinimumWidth(360)
        side.setMaximumWidth(600)
        self.instrument_panel=side
        right=QVBoxLayout(side)
        title=QLabel("Инструменты")
        title.setStyleSheet("font-weight:700")
        right.addWidget(title)
        # Preserve private controls for internal navigation and existing tests.
        # Do not expose a manual entry that bypasses the user's fixed universe.
        self.symbol_edit=QLineEdit(side)
        self.symbol_edit.returnPressed.connect(self._select)
        self.symbol_edit.hide()
        self.mic=QComboBox(side)
        self.mic.setEditable(True)
        self.mic.addItems(("MISX","RTSX","XCME","XCEC","XNYM","IFEU"))
        self.mic.hide()
        self.open_button=QPushButton("Открыть инструмент",side)
        self.open_button.clicked.connect(self._select)
        self.open_button.hide()
        self.examples=QListWidget(side)
        self.examples.setObjectName("instrumentList")
        self.examples.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.examples.itemActivated.connect(self._example)
        self.examples.itemClicked.connect(self._example)
        right.addWidget(self.examples,1)
        self.universe_info=QLabel(side)
        self.universe_info.setWordWrap(True)
        self.universe_info.setStyleSheet("color:#a2a9b3;font-size:11px")
        right.addWidget(self.universe_info)
        self.universe_info.hide()  # Remove verbose technical footer from sidebar.
        self._populate_universe()
        split.addWidget(side)
        split.setStretchFactor(0,1)
        split.setSizes([1000,460])
        self.instrument_splitter=split
        outer.addWidget(split,1)
        self.status=QLabel("Только чтение · торговые заявки отключены")
        outer.addWidget(self.status)
        self.status.hide()  # Internal messages are logged; load errors appear on chart.
        root.setStyleSheet("""
          QWidget {background:#191919;color:#e7e7e7;font-family:'Segoe UI';}
          QPushButton,QLineEdit,QComboBox,QListWidget {background:#282828;
            color:#e7e7e7;border:1px solid #444;border-radius:4px;padding:7px;}
          QPushButton:hover {background:#343434;}
          QListWidget#instrumentList {border:2px solid #687383;
            border-radius:5px;background:#252525;padding:3px;outline:none;}
          QListWidget#instrumentList::item {border:1px solid transparent;
            border-radius:4px;margin:1px;padding:2px;}
          QListWidget#instrumentList::item:selected {background:#2b4b68;
            border:1px solid #86baf0;border-radius:4px;}
          QListWidget#instrumentList::item:selected:!active {background:#30465b;
            border:1px solid #8d9db0;}
        """)
        self._populate_universe()


    def _add_instrument_item(self, entry, group):
        """Show one curated instrument without FVG scanning or signal badges."""
        pending=(not entry.verified and not self.catalog_ready and
                 not self.catalog_error and group!=RU_STOCKS)
        state=(" · поиск Finam…" if pending else
               " · каталог недоступен" if self.catalog_error and group!=RU_STOCKS else
               " · не подтверждён Finam" if not entry.verified else "")
        if entry.verified and "сохранённый список; сверка Finam" in entry.source:
            state+=" · кэш"
        if entry.verified and "экспирация не подтверждена" in entry.source:
            state+=" · эксп. ?"
        elif entry.verified and group!=RU_STOCKS:
            known=re.search(r"(?:действует до |новая дата )(\\d{4}-\\d{2}-\\d{2})",
                            entry.source)
            if known:
                stamp=known.group(1)
                state+=" · до "+stamp[8:10]+"."+stamp[5:7]
        item=QListWidgetItem(entry.label+" · "+entry.name+state)
        item.setData(Qt.ItemDataRole.UserRole,entry.symbol if entry.verified else "")
        if not entry.verified:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(Qt.GlobalColor.gray)
            item.setToolTip("Точный тикер@MIC не подтверждён Finam")
        else:
            item.setToolTip(entry.symbol)
        self.examples.addItem(item)

    def _populate_universe(self):
        """Unfiltered list of existing US/RU futures and top-10 MOEX stocks."""
        selected=self.examples.currentItem()
        symbol=selected.data(Qt.ItemDataRole.UserRole) if selected else None
        label=selected.text().split(" · ",1)[0] if selected else ""
        scroll=self.examples.verticalScrollBar().value()
        self.examples.setUpdatesEnabled(False)
        self.examples.clear()
        for group in (US_FUTURES,RU_FUTURES,RU_STOCKS):
            header=QListWidgetItem(GROUP_LABELS[group])
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setForeground(Qt.GlobalColor.gray)
            self.examples.addItem(header)
            for entry in self.universe[group]:
                self._add_instrument_item(entry,group)
        if label:
            for i in range(self.examples.count()):
                item=self.examples.item(i)
                if (symbol and item.data(Qt.ItemDataRole.UserRole)==symbol or
                        item.text().split(" · ",1)[0]==label):
                    self.examples.setCurrentItem(item)
                    break
        self.examples.verticalScrollBar().setValue(scroll)
        self.examples.setUpdatesEnabled(True)

    def _daily_catalog_refresh(self):
        if self.closing or not read_secret():
            return
        now=datetime.now(timezone.utc)
        if self._catalog_success_slot != slot_date(now):
            self._refresh_catalog()

    def _refresh_catalog(self, *, force=False):
        if self.closing or not read_secret() or self.catalog_worker is not None:
            return
        now=datetime.now(timezone.utc)
        if (not force and self._catalog_attempt_at is not None and
                (now-self._catalog_attempt_at).total_seconds()<600):
            return
        self._catalog_attempt_at=now
        self.catalog_error=""
        self.catalog_worker=FinamCatalogWorker(self.service.api,self,refresh_ranking=not self._ranking_attempted)
        self._ranking_attempted=True
        self.catalog_worker.futures_ready.connect(self._futures_ready)
        self.catalog_worker.loaded.connect(self._catalog_loaded)
        self.catalog_worker.failed.connect(self._catalog_failed)
        self.catalog_worker.progress.connect(self._catalog_progress)
        self.catalog_worker.finished.connect(self._catalog_finished)
        self.catalog_worker.start()

    def _futures_ready(self, futures):
        if self.closing or not isinstance(futures,dict):
            return
        for group, contracts in ((US_FUTURES,US_CONTRACTS),
                                 (RU_FUTURES,RU_CONTRACTS)):
            updates=[]
            for contract, prior in zip(contracts,self.universe[group]):
                verified=futures.get(group+":"+contract.display)
                updates.append(verified if verified and verified.verified else prior)
            self.universe[group]=updates
        self._populate_universe()

    def _catalog_loaded(self,assets):
        if self.closing:return
        self.catalog_ready=True
        payload=assets if isinstance(assets,dict) else {}
        self.catalog_error=payload.get("finam_error","")
        raw=payload.get("assets",assets if isinstance(assets,list) else [])
        self.universe,self.ranking_status=compose_universe(raw)
        if isinstance(payload.get("stocks"),list) and len(payload["stocks"])==10:
            self.universe[RU_STOCKS]=payload["stocks"]
            self.ranking_status=payload.get("ranking_status",self.ranking_status)
        fallback=self.futures_list_cache.load()
        fresh=payload.get("futures",{})
        for group in (US_FUTURES,RU_FUTURES):
            merged=[]
            for contract in self.universe[group]:
                key=group+":"+contract.label
                resolved=fresh.get(key)
                merged.append(resolved if resolved and resolved.verified
                              else fallback.get(key,contract))
            self.universe[group]=merged
        # An outage must not be counted as a completed daily refresh.
        # The ten-minute attempt throttle will retry without hammering APIs.
        if (not self.catalog_error and "MOEX недоступна" not in self.ranking_status
                and "ошибка MOEX" not in self.ranking_status
                and FuturesListCache.complete(fresh)):
            self._catalog_success_slot=slot_date(datetime.now(timezone.utc))
        self._populate_universe()
        if self._pending_restore:
            restored=self._pending_restore
            self._pending_restore=None
            matched=payload.get("roll_map",{}).get(restored.upper(),restored)
            if is_in_universe(matched,self.universe):
                ticker,mic=matched.split("@",1)
                self.symbol_edit.setText(ticker)
                self.mic.setCurrentText(mic)
                self.ui_prefs.setValue("ticker",ticker)
                self.ui_prefs.setValue("mic",mic)
                if matched.upper()!=restored.upper():
                    self.status.setText("Контракт автоматически переключён за день до экспирации: "+matched)
            else:
                self.status.setText("Сохранённый контракт недоступен. Открыта первая акция из топ-10.")
                fallback=next((i for i in self.universe[RU_STOCKS] if i.verified),None)
                if fallback:
                    ticker,mic=fallback.symbol.split("@",1)
                    self.symbol_edit.setText(ticker)
                    self.mic.setCurrentText(mic)
            self._load(refresh=False)
            self._start_stream()
        elif self.current_view:
            # An already open futures chart must follow a newly confirmed
            # roll when the daily refresh runs without restarting the app.
            current_symbol=symbol_name(*self.current_view[:2])
            moved=payload.get("roll_map",{}).get(current_symbol.upper())
            if moved and moved.upper()!=current_symbol.upper() and is_in_universe(moved,self.universe):
                ticker,mic=moved.split("@",1)
                self.symbol_edit.setText(ticker)
                self.mic.setCurrentText(mic)
                self.ui_prefs.setValue("ticker",ticker)
                self.ui_prefs.setValue("mic",mic)
                self.status.setText("Контракт переключён после ежедневной проверки Finam: "+moved)
                self._load(refresh=True)
                self._start_stream()

    def _catalog_progress(self,message):
        if not self.closing and self.catalog_worker is not None:
            self.universe_info.setText(message+"\nАкции: "+self.ranking_status)

    def _catalog_failed(self,error):
        if self.closing:return
        # The worker already recorded structured failure context before
        # reducing the signal to a safe display string; don't overwrite it.
        self.catalog_error=error
        self._populate_universe()
        self.status.setText("Finam: "+error)
        if self._pending_restore:
            self._pending_restore=None
            self._load(refresh=False)
            self._start_stream()

    def _catalog_finished(self):
        worker=self.sender()
        if worker is self.catalog_worker:
            self.catalog_worker=None
        worker.deleteLater()
        if self.closing:
            self._finish_close()

    def _settings(self):
        dialog=SettingsDialog(self.indicator_settings,self.app_settings,self,
                              finam_only=True,token_action=self._token_dialog)
        dialog.applied.connect(self._apply_settings)
        dialog.exec()

    def _apply_settings(self,indicator,application):
        recalculate=self.indicator_settings.calculation_key()!=indicator.calculation_key()
        self.indicator_settings=indicator
        # Both transports remain enabled even when importing older settings.
        self.app_settings=replace(application,auto_updates=True,allow_http=True,
                                  refresh_seconds=HTTP_REFRESH_SECONDS)
        self.settings_store.write(indicator,self.app_settings)
        self.chart.set_settings(indicator)
        self.chart.set_chart_colors(self.app_settings)
        self.http_timer.setInterval(self.app_settings.refresh_seconds*1000)
        self._schedule_http()
        self._start_stream()
        if recalculate:
            self._load(refresh=False)

    def _token_dialog(self,parent=None):
        token, ok=QInputDialog.getText(parent or self,"Finam Trade API",
            "Введите secret key (не access key). Он сохранится в системном хранилище Windows.",
            QLineEdit.EchoMode.Password)
        if not ok or not token:
            return
        try:
            save_secret(token)
        except Exception as exc:
            QMessageBox.warning(parent or self,"Хранилище ключей",type(exc).__name__)
            return
        # The existing socket and account-scoped asset metadata belong to
        # the previous token. Reconnect with the new secret from keyring.
        self._stop_stream()
        self.service.api.invalidate_session()
        invalidate=getattr(self.service,"invalidate_network_cache",None)
        if callable(invalidate):invalidate()
        self.status.setText("Токен сохранён в Windows Credential Manager; проверка подключения…")
        self._load(refresh=True)
        self._start_stream()
        self._refresh_catalog(force=True)
        self._schedule_http()

    def _backup_stores(self):
        if not self.closing:
            schedule=getattr(self.service,"backup_if_due_async",None)
            if callable(schedule):
                schedule()

    def _startup(self):
        if not read_secret():
            self.status.setText("Токен Finam не введён. Откройте настройки (⚙).")
            self._indicator()
            return
        self._refresh_catalog(force=True)
        self._schedule_http()
        if self._pending_restore:
            self.status.setText("Finam: поиск сохранённого инструмента в каталоге…")
            return
        self._load(refresh=True)
        self._start_stream()

    def _example(self,item):
        symbol=item.data(Qt.ItemDataRole.UserRole)
        if not symbol or "@" not in symbol:
            return
        ticker,mic=symbol.split("@",1)
        self.symbol_edit.setText(ticker)
        self.mic.setCurrentText(mic)
        self._select()

    def _select(self):
        try:
            sym=symbol_name(self.symbol_edit.text(),self.mic.currentText())
        except ValueError as exc:
            self.status.setText(str(exc));return
        ticker,mic=sym.split("@")
        if not is_in_universe(ticker+"@"+mic,self.universe):
            self.status.setText("Инструмент отсутствует в выбранном списке или не подтверждён Finam")
            return
        # A user's choice takes precedence over a saved startup selection
        # whose asynchronous catalog lookup may still be running.
        self._pending_restore=None
        self.ui_prefs.setValue("ticker",ticker)
        self.ui_prefs.setValue("mic",mic)
        self._load(refresh=False)
        self._start_stream()
        self._schedule_http()

    @staticmethod
    def _exchange_time(value, mic, *, seconds=False):
        """Display sourced timestamps in the venue's timezone; never change OHLC."""
        clock=exchange_clock(mic)
        try:
            stamp=value if isinstance(value,datetime) else parse_instant(value)
            if stamp.tzinfo is None:
                return str(value)  # Source has no explicit timezone: don't guess.
            label=clock.format(stamp,seconds=seconds)
            return label if clock.known else label+" (часовой пояс MIC не определён)"
        except (ValueError,TypeError,AttributeError,OverflowError):
            return str(value)

    def _load(self,refresh=False,events=()):
        if self.closing:
            return
        try:
            ticker,mic=symbol_name(self.symbol_edit.text(),self.mic.currentText()).split("@")
        except ValueError as exc:
            self.status.setText(str(exc));return
        request=(ticker,mic,self.tf.currentText(),refresh,tuple(events))
        if self.current != (ticker,mic):
            # Previous symbol may have been manually Y-zoomed/panned (e.g.
            # 116–117 RUB). Its scale and X offset cannot be inherited by a
            # different security trading at 6,000+ RUB: reset both rulers.
            # The selected timeframe itself is preserved.
            self.chart.reset_view()
            self.chart.set_price_tick(None)
        self.chart.set_exchange(mic,ticker)
        self.current=(ticker,mic)
        self.current_view=request[:3]
        if self.displayed_view != self.current_view:
            self.analysis,self.parents=None,{}
            self.displayed_view=None
            self.chart.clear_analysis(f"Загрузка {ticker}@{mic} · {request[2]}…")
            self.feed.setText(f"Finam · {ticker}@{mic} · {request[2]} · ожидаем данные")
        self.ui_prefs.setValue("tf",self.tf.currentText())
        if self.loader is not None:
            pending = self.pending_load
            if pending is not None and pending[:2] == request[:2]:
                request = (*request[:3], refresh or pending[3], pending[4] + request[4])
            self.pending_load=request
            worker = self.loader
            if not worker.superseded and (worker.ticker, worker.mic, worker.timeframe) != self.current_view:
                worker.superseded = True
                worker.requestInterruption()
            if worker.superseded:
                self.status.setText(f"Finam: открытие {ticker}@{mic} · {request[2]}")
            return
        # HTTP checks and chart loads share one worker. A pending HTTP check
        # is served by this load or remains queued behind an active worker.
        self._http_refresh_pending=False
        defer_chart = self.isMinimized()
        self._restore_refresh_pending = defer_chart
        market=(self.service.for_market(mic) if callable(getattr(self.service,"for_market",None))
                else self.service)
        self.loader=FinamLoadWorker(market,ticker,mic,request[2],
              self.indicator_settings,allow_http=True,
              refresh=refresh,events=request[4],defer_chart=defer_chart)
        self.loader.loaded.connect(self._loaded)
        self.loader.failed.connect(self._failed)
        self.loader.progress.connect(self._load_progress)
        self.loader.finished.connect(self._load_finished)
        self.status.setText("Finam: открытие "+ticker+"@"+mic+" · "+request[2])
        self._indicator()
        self.loader.start()

    def _loaded(self,main,parents,info):
        if (self.closing or self.isMinimized() or not self._current_load_signal() or
                (info["symbol"], info["board"], info["tf"]) != self.current_view):
            return
        # Read the user's latest view on delivery, including gestures made
        # while HTTP/reconnect work was in flight. New charts still reframe.
        saved_view=(self.chart.view_state() if self.displayed_view==self.current_view
                    and self.chart.analysis is not None else None)
        self.analysis,self.parents=main,parents
        self.displayed_view=self.current_view
        provider="Finam"
        self.chart.instrument_header=info["symbol"]+"@"+info["board"]+" · "+provider
        self.chart.set_exchange(info["board"],info["symbol"])
        # Unknown step must never silently inherit the previous instrument's.
        self.chart.set_price_tick(info.get("price_tick_size"))
        self.chart.set_analysis(main,parents)
        if saved_view is not None:
            # Same symbol/TF: keep the user's current pan, fractional zoom,
            # right whitespace and manual price scale on every refresh.
            self.chart.restore_view(saved_view)
        else:
            # First successful display of a symbol/TF (including startup):
            # show the last real candle at the center of the time plot.
            self.chart.center_latest()
        self.chart.empty_text=""
        self.status.setText(provider+" · "+info["tf"]+" · "+str(len(main.candles))+
            " свечей · "+info["warning"])
        log_snapshot(symbol=info["symbol"]+"@"+info["board"],
                     timeframe=info["tf"],asof=info.get("asof"),
                     delay_seconds=info.get("feed_delay_seconds"))
        delay=info.get("feed_delay_seconds",0)//60
        permissions_known=getattr(getattr(self.service,"api",None),"permissions_status","unknown")=="verified"
        delay_label=("разрешённая задержка " if permissions_known
                     else "права не подтверждены; расчётная задержка ")
        self.feed.setText(provider+" · наблюдение "+
                          self._exchange_time(info["asof"],info["board"]) +
                          " · "+delay_label+str(delay)+" мин · только чтение")
        self._indicator()

    def _current_load_signal(self):
        worker=self.sender()
        # Interruption can reset when QThread finishes, before Qt delivers a queued signal.
        return worker is None or (
            worker is self.loader and not worker.superseded and
            (worker.ticker, worker.mic, worker.timeframe) == self.current_view)

    def _load_progress(self,message):
        if not self.closing and not self.isMinimized() and self._current_load_signal():
            self.status.setText(message)

    def _failed(self,reason):
        if self.closing or not self._current_load_signal():
            return
        symbol=(self.current_view[0]+"@"+self.current_view[1]
                if self.current_view else "")
        log_event("chart_load_failed",symbol=symbol,error=reason)
        if self.isMinimized():
            return
        self.status.setText("Finam: "+reason)
        if self.displayed_view != self.current_view:
            ticker,mic,tf=self.current_view
            self.chart.clear_analysis(f"Не удалось загрузить {ticker}@{mic} · {tf}\n{reason}")
            self.feed.setText(f"Finam · {ticker}@{mic} · {tf} · история не загружена")
        self._indicator()

    def _load_finished(self):
        thread=self.sender()
        if thread is None or thread is not self.loader:
            return
        self.loader=None
        thread.deleteLater()
        if self.closing:
            self._finish_close()
            return
        if self.pending_load:
            req=self.pending_load
            self.pending_load=None
            self.symbol_edit.setText(req[0])
            self.mic.setCurrentText(req[1])
            blocked=self.tf.blockSignals(True)
            self.tf.setCurrentText(req[2])
            self.tf.blockSignals(blocked)
            repeat_refresh = (thread.superseded and thread.refresh and
                              (thread.ticker, thread.mic) == req[:2])
            self._load(refresh=req[3] or self._http_refresh_pending or repeat_refresh,
                       events=req[4] + self._take_pending_bars())
        elif self._http_refresh_pending:
            self._http_refresh()
        elif self._restore_refresh_pending and not self.isMinimized():
            self._refresh_restored_chart()
        elif self.pending_bars:
            self.feed_timer.start()
        self._indicator()

    def _make_stream(self,ticker,mic):
        return FinamStreamWorker(ticker,mic,api=self.service.api)

    def _start_stream(self):
        if self.closing or not read_secret():
            self._indicator();return
        try:
            ticker,mic=symbol_name(self.symbol_edit.text(),self.mic.currentText()).split("@")
        except ValueError:
            return
        if self.stream is not None and (self.stream.ticker,self.stream.mic)==(ticker,mic):
            return
        self._stop_stream()
        if getattr(self,'_retiring',()):
            self._pending_stream=True
            return  # Finish closing the previous instrument before connecting.
        self._pending_stream=False
        try:
            self.stream=self.stream_factory(ticker,mic)
        except sqlite3.DatabaseError:
            # Never swallow an SQLite
            # failure as a silent failed WebSocket connection.
            self.stream=None
            self.stream_state="Ошибка локальной базы данных"
            self.status.setText(
                "SQLite: повреждена база Finam"+
                ". Исходный файл сохранён; используйте резервную копию.")
            self._indicator()
            return
        self.stream.event.connect(self._event)
        self.stream.finished.connect(self._stream_finished)
        self.stream_ready=False
        self.stream_epoch=0
        self.stream_state="Подключение WebSocket"
        self.stream.start()
        self._indicator()

    def _stop_stream(self):
        if self.stream is not None:
            old,self.stream=self.stream,None
            # Prevent a QThread deletion while transport closes.
            self._retiring=getattr(self,"_retiring",[])
            self._retiring.append(old)
            old.finished.connect(partial(self._retired,old))
            old.stop()
            finished=getattr(old,'isFinished',None)
            if callable(finished) and finished():self._retired(old)
        self.stream_ready=False
        self.pending_bars.clear()
        self.feed_timer.stop()

    def _retired(self,worker):
        if worker not in self._retiring:return
        self._retiring.remove(worker)
        worker.deleteLater()
        if self.closing:
            self._finish_close()
        elif self._pending_stream and not self._retiring:
            self._start_stream()

    def _stream_finished(self):
        thread=self.sender()
        if thread is self.stream:
            self.stream=None
            self.stream_ready=False
            self.stream_state="Поток остановлен"
            thread.deleteLater()
        if self.closing:
            self._finish_close()
        else:
            self._indicator()

    def _event(self,event):
        if self.closing or self.stream is None or (
            event.symbol,event.board)!=(self.stream.ticker,self.stream.mic):
            return
        if event.epoch < getattr(self,"stream_epoch",0):
            return
        new_epoch=event.epoch > getattr(self,"stream_epoch",0)
        if new_epoch:
            self.stream_epoch=event.epoch
            self.pending_bars.clear()
        if event.kind=="status":
            previous_state=self.stream_state
            self.stream_state=event.state
            if event.state in ("resync","reconnecting","unavailable"):
                mark=getattr(self.service,"mark_stream_reconnect",None)
                if callable(mark):mark(symbol_name(event.symbol,event.board))
            if event.state=="ready" and (previous_state!="ready" or new_epoch):
                QTimer.singleShot(0,self._http_refresh)
            if previous_state != event.state and event.state in ("unavailable","reconnecting"):
                log_event("stream_"+event.state,
                          symbol=event.symbol+"@"+event.board,error=event.detail)
            if event.detail:
                self.stream_error=event.detail
            if event.state=="ready":
                self.stream_ready=True
                self.stream_error=""
            if event.state in ("connecting","resync","reconnecting","unavailable"):
                self.stream_ready=False
            if event.state=="unavailable":
                self.status.setText("Finam: "+event.detail)
        elif event.kind=="quote":
            ingest=getattr(self.service,"ingest_quote",None)
            if callable(ingest):ingest(event)
            q=event.quote
            last=q.get("last")
            if last is not None and not self.isMinimized():
                instant=q.get("timestamp")
                stamp=(self._exchange_time(instant,event.board,seconds=True)
                       if instant is not None else "время сделки не указано")
                provider="Finam"
                self.feed.setText(f"{provider} WebSocket · {event.symbol}@{event.board} · цена {last} · "+stamp)
        elif event.kind=="candles":
            for c in event.candles:
                self.pending_bars[(event.timeframe,c.time)]=event.__class__(
                    event.kind,event.symbol,event.board,event.epoch,event.received_at,
                    timeframe=event.timeframe,candles=(c,))
            if not self.feed_timer.isActive():
                self.feed_timer.start()
        self._indicator()

    def _take_pending_bars(self):
        events = tuple(self.pending_bars.values())
        self.pending_bars.clear()
        return events

    def _flush(self):
        if self.loader is not None or self.closing or not self.pending_bars:
            return
        self._load(refresh=False,events=self._take_pending_bars())

    def _http_refresh(self):
        if self.closing or not read_secret():
            return
        if self.loader is not None:
            self._http_refresh_pending=True
            return
        self._http_refresh_pending=False
        self._load(refresh=True,events=self._take_pending_bars())

    def _refresh_restored_chart(self):
        if self.loader is not None or not self._restore_refresh_pending:
            return
        self.feed_timer.stop()
        self._load(events=self._take_pending_bars())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange or getattr(self, 'closing', True):
            return
        if self.isMinimized():
            self._restore_refresh_pending = self.current_view is not None
        elif event.oldState() & Qt.WindowState.WindowMinimized:
            self._refresh_restored_chart()
            self._indicator()

    def _schedule_http(self):
        if not self.closing and read_secret():
            if not self.http_timer.isActive():
                self.http_timer.start()
        else:
            self.http_timer.stop()

    def _indicator(self):
        if not hasattr(self,"channel") or self.isMinimized():
            return
        provider="FINAM"
        if self.stream_ready:
            label,color="FINAM · ПОТОК","#22553b"
        elif self.stream_state=="reconnecting":
            label,color="FINAM · ПЕРЕПОДКЛ.","#5b492b"
        elif self.stream is not None:
            label,color="FINAM · ПОДКЛ.","#52452e"
        elif not read_secret():
            label,color="FINAM · НЕТ ТОКЕНА","#4a3434"
        else:
            label,color="FINAM · НЕТ ПОТОКА","#493131"
        if self.loader is not None and self.loader.allow_http and self.loader.refresh:
            label,color="FINAM · HTTP","#2a415b"
        label=label.replace("FINAM",provider)
        self.channel.setText(label)
        self.channel.setStyleSheet(f"background:{color};border:1px solid #555;"
                                   "border-radius:5px;color:#f0f0f0;padding:8px;font-weight:700")
        self.channel.setToolTip("Поставщик: Finam Trade API"+
            "; WebSocket и HTTP включены"+
            ("\nПоследняя ошибка: "+self.stream_error if self.stream_error else ""))

    def _finish_close(self):
        if (self.loader is None and self.stream is None and
                self.catalog_worker is None and
                not getattr(self,"_retiring",[])):
            QApplication.instance().quit()

    def closeEvent(self,event):
        self.closing=True
        self.feed_timer.stop()
        self.http_timer.stop()
        self.daily_catalog_timer.stop()
        self.storage_backup_timer.stop()
        self._stop_stream()
        if self.loader is not None:
            self.loader.requestInterruption()
        if self.catalog_worker is not None:
            self.catalog_worker.requestInterruption()
        if (self.loader is not None or self.catalog_worker is not None or
                getattr(self,"_retiring",[])):
            event.ignore()
            self.hide()
        else:
            event.accept()


def run():
    set_windows_application_id()
    app=QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FlowDesk")
    app.setWindowIcon(application_icon())
    try:
        window=FinamWindow()
    except sqlite3.DatabaseError:
        # Never silently delete/recreate a corrupt candle database. The
        # original remains untouched; the owner can recover a verified copy
        # to a separate file using the storage maintenance command.
        from .storage_safety import snapshots
        path=Path.home()/".flowdesk"/"finam_candles_v1.sqlite3"
        count=len(snapshots(path))
        QMessageBox.critical(
            None,"FlowDesk · ошибка базы данных",
            "Не удалось открыть локальную базу свечей SQLite.\n"
            "Файл не удалён и не перезаписан.\n"
            "Доступных резервных копий: "+str(count)+".\n"
            "Закройте приложение и проверьте копии в папке "
            ".flowdesk/backups/finam_candles_v1.\n"
            "Восстанавливайте данные только в новый файл, сохраняя исходный.")
        return 2
    window.show()
    return app.exec()
