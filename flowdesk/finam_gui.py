"""Finam-only FlowDesk entrypoint. No MOEX feed, exchange picker or order APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from pathlib import Path
import re
import sys

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
    QListWidget, QListWidgetItem, QInputDialog, QSplitter, QFormLayout, QFrame
)

from . import __version__
from .app_icon import application_icon, set_windows_application_id
from .finam_api import read_secret, save_secret, delete_secret, symbol_name, parse_instant
from .exchange_time import exchange_clock
from .finam_market import FinamMarketService
from .finam_universe import (
    US_FUTURES, RU_STOCKS, RU_FUTURES, GROUP_LABELS,
    compose_universe, cached_top20, is_in_universe
)
from .finam_catalog_worker import FinamCatalogWorker
from .diagnostics_log import log_event, log_snapshot
from .finam_stream import FinamStreamWorker
from .gui import CandleChart, TimeframeBar
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
                 *, allow_http=False, refresh=False, events=()):
        super().__init__()
        self.service, self.ticker, self.mic = service, ticker, mic
        self.timeframe, self.settings = timeframe, settings
        self.allow_http, self.refresh, self.events = allow_http, refresh, events

    def run(self):
        try:
            if self.events:
                self.service.ingest_stream(self.ticker,self.mic,self.events)
            if self.isInterruptionRequested():
                return
            result = self.service.view(self.ticker,self.mic,self.timeframe,
                allow_http=self.allow_http,refresh=self.refresh,
                settings=self.settings,progress=self.progress.emit)
            if self.isInterruptionRequested():
                return
            # Asset metadata is read-only and independent of the HTTP *candle*
            # checkbox. Resolve its true tick size in the worker, never on Qt.
            getter = getattr(getattr(self.service, "api", None), "asset_tick_size", None)
            if callable(getter):
                try:
                    step = getter(symbol_name(self.ticker,self.mic))
                except Exception:
                    step = None  # Price data may still load from the local cache.
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
        self.setWindowTitle(f"FlowDesk {__version__} · Finam Trade API · только данные")
        self.setWindowIcon(application_icon())
        self.resize(1420,840)
        self.setMinimumSize(1040,650)
        self.service = service or FinamMarketService()
        self.stream_factory = stream_factory or (lambda ticker,mic: FinamStreamWorker(ticker,mic,api=self.service.api))
        self.settings_store = settings_store or SettingsStore()
        self.indicator_settings, self.app_settings = self.settings_store.read()
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
        self.closing = False
        self.analysis = None
        self.parents = {}
        self.catalog_worker = None
        self.catalog_ready = False
        self.catalog_error = ""
        self.moex_status=""
        self.universe, self.ranking_status = compose_universe()
        self._make_ui()
        self.feed_timer = QTimer(self)
        self.feed_timer.setSingleShot(True)
        self.feed_timer.setInterval(1600)
        self.feed_timer.timeout.connect(self._flush)
        self.http_timer = QTimer(self)
        self.http_timer.setInterval(self.app_settings.refresh_seconds*1000)
        self.http_timer.timeout.connect(self._http_refresh)
        ticker=str(self.ui_prefs.value("ticker","SBER"))
        mic=str(self.ui_prefs.value("mic","MISX"))
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
        tf = str(self.ui_prefs.value("tf","H1"))
        blocked=self.tf.blockSignals(True)
        self.tf.setCurrentText(tf if tf in self.tf.buttons else "H1")
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
        self.tf=TimeframeBar()
        self.tf.set_theme("dark")
        self.tf.currentTextChanged.connect(lambda _:self._load(refresh=False))
        header.addWidget(self.tf)
        header.addStretch()
        self.channel=QLabel()
        self.channel.setMinimumWidth(190)
        self.channel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.channel)
        self.settings_btn=QPushButton("⚙")
        self.settings_btn.setToolTip("Настройки индикатора v2.11")
        self.settings_btn.clicked.connect(self._settings)
        header.addWidget(self.settings_btn)
        outer.addLayout(header)
        bar=QHBoxLayout()
        self.auto=QCheckBox("Авто")
        self.auto.setChecked(self.app_settings.auto_updates)
        self.auto.setToolTip("Включает поток свечей и котировок Finam по WebSocket")
        self.auto.toggled.connect(self._auto_toggled)
        bar.addWidget(self.auto)
        self.http=QCheckBox("HTTP")
        self.http.setChecked(self.app_settings.allow_http)
        self.http.setToolTip("Разрешение только на HTTP-загрузку истории и восстановление; авторизация Finam выполняется независимо.")
        self.http.toggled.connect(self._http_toggled)
        bar.addWidget(self.http)
        bar.addSpacing(8)
        latest=QPushButton("Последние · End")
        latest.clicked.connect(lambda:self.chart.latest())
        bar.addWidget(latest)
        auto_price=QPushButton("Автоцена · F")
        auto_price.clicked.connect(self.chart_auto_price_later)
        bar.addWidget(auto_price)
        self.token_btn=QPushButton("Токен Finam")
        self.token_btn.clicked.connect(self._token_dialog)
        bar.addWidget(self.token_btn)
        bar.addStretch()
        outer.addLayout(bar)
        self.feed=QLabel("Finam: ожидаем данные")
        self.feed.setStyleSheet("color:#a4a4a4")
        outer.addWidget(self.feed)
        self.feed.hide()  # Technical observation belongs in diagnostics.log.
        split=QSplitter(Qt.Orientation.Horizontal)
        self.chart=CandleChart()
        self.chart.set_theme("dark")
        self.chart.set_settings(self.indicator_settings)
        self.chart.empty_text="Добавьте токен Финама и выберите инструмент"
        split.addWidget(self.chart)
        side=QFrame()
        side.setMaximumWidth(290)
        side.setMinimumWidth(235)
        right=QVBoxLayout(side)
        title=QLabel("Инструменты · только выбранные")
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
        self.search=QLineEdit(side)
        self.search.setPlaceholderText("Поиск в выбранном списке…")
        self.search.textChanged.connect(self._filter_universe)
        right.addWidget(self.search)
        self.examples=QListWidget(side)
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
        outer.addWidget(split,1)
        self.status=QLabel("Только чтение · торговые заявки отключены")
        outer.addWidget(self.status)
        root.setStyleSheet("""
          QWidget {background:#191919;color:#e7e7e7;font-family:'Segoe UI';}
          QPushButton,QLineEdit,QComboBox,QListWidget {background:#282828;
            color:#e7e7e7;border:1px solid #444;border-radius:4px;padding:7px;}
          QPushButton:hover {background:#343434;}
          QListWidget::item:selected {background:#2b4b68;}
        """)

    def _populate_universe(self):
        """Only the 8+20+10 requested rows; no unrelated demo assets."""
        current=self.search.text().strip().lower() if hasattr(self,"search") else ""
        self.examples.clear()
        self._sections=[]
        for group in (US_FUTURES,RU_STOCKS,RU_FUTURES):
            header=QListWidgetItem(GROUP_LABELS[group])
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setForeground(Qt.GlobalColor.gray)
            self.examples.addItem(header)
            self._sections.append(header)
            for entry in self.universe[group]:
                pending=not self.catalog_ready and not self.catalog_error and group!=RU_STOCKS
                state=(" · поиск Finam…" if pending else
                       " · каталог недоступен" if self.catalog_error and group!=RU_STOCKS else
                       " · не подтверждён Finam" if not entry.verified else "")
                if entry.verified and "экспирация не подтверждена" in entry.source:
                    state+=" · эксп. ?"
                elif entry.verified and group!=RU_STOCKS:
                    known=re.search(r"(?:действует до |новая дата )(\d{4}-\d{2}-\d{2})",
                                    entry.source)
                    if known:
                        stamp=known.group(1)
                        state+=" · до "+stamp[8:10]+"."+stamp[5:7]
                row=QListWidgetItem(entry.label+" · "+entry.name+state)
                row.setData(Qt.ItemDataRole.UserRole,entry.symbol if entry.verified else "")
                if not entry.verified:
                    row.setFlags(Qt.ItemFlag.NoItemFlags)
                    row.setForeground(Qt.GlobalColor.gray)
                    row.setToolTip("Для открытия необходим точный тикер@MIC из каталога Finam. Не подменяем похожим контрактом.")
                else:
                    row.setToolTip(entry.symbol)
                self.examples.addItem(row)
        self.universe_info.setText(
            "Акции: "+self.ranking_status+
            " · графики только Finam; обороты акций и фьючерсов РФ — MOEX ISS."+
            (" · "+self.moex_status if self.moex_status else "")+
            " · фьючерсы: переход за день до экспирации; без даты автопереход выключен."+
            (" · Каталог: "+self.catalog_error if self.catalog_error else "")
        )
        self._filter_universe(current)

    def _filter_universe(self,text):
        term=text.strip().lower()
        visible_count=0
        pending_header=None
        for i in range(self.examples.count()):
            item=self.examples.item(i)
            if item in getattr(self,"_sections",()):
                if pending_header is not None:
                    pending_header.setHidden(visible_count==0)
                pending_header=item
                visible_count=0
                continue
            matched=not term or term in item.text().lower()
            item.setHidden(not matched)
            visible_count+=int(matched)
        if pending_header is not None:
            pending_header.setHidden(visible_count==0)

    def _refresh_catalog(self):
        if self.closing or not read_secret() or self.catalog_worker is not None:
            return
        self.catalog_error=""
        self.catalog_worker=FinamCatalogWorker(self.service.api,self)
        self.catalog_worker.loaded.connect(self._catalog_loaded)
        self.catalog_worker.failed.connect(self._catalog_failed)
        self.catalog_worker.progress.connect(self._catalog_progress)
        self.catalog_worker.finished.connect(self._catalog_finished)
        self.catalog_worker.start()

    def _catalog_loaded(self,assets):
        if self.closing:return
        self.catalog_ready=True
        payload=assets if isinstance(assets,dict) else {}
        self.catalog_error=payload.get("finam_error","")
        raw=payload.get("assets",assets if isinstance(assets,list) else [])
        self.universe,self.ranking_status=compose_universe(raw)
        if isinstance(payload.get("stocks"),list) and len(payload["stocks"])==20:
            self.universe[RU_STOCKS]=payload["stocks"]
            self.ranking_status=payload.get("ranking_status",self.ranking_status)
        for group in (US_FUTURES,RU_FUTURES):
            self.universe[group]=[
                payload.get("futures",{}).get(group+":"+contract.label,contract)
                for contract in self.universe[group]
            ]
        self.moex_status=payload.get("moex_status","")
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
                self.status.setText("Сохранённый контракт недоступен. Открыта первая акция из топ-20.")
                fallback=next((i for i in self.universe[RU_STOCKS] if i.verified),None)
                if fallback:
                    ticker,mic=fallback.symbol.split("@",1)
                    self.symbol_edit.setText(ticker)
                    self.mic.setCurrentText(mic)
            self._load(refresh=False)
            self._start_stream()

    def _catalog_progress(self,message):
        if not self.closing and self.catalog_worker is not None:
            self.universe_info.setText(message+"\nАкции: "+self.ranking_status)

    def _catalog_failed(self,error):
        if self.closing:return
        log_event("catalog_worker_failed",error=error)
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

    def chart_auto_price_later(self):
        self.chart.auto_price()

    def _save_settings(self):
        self.app_settings=ApplicationSettings(
            self.auto.isChecked(),self.app_settings.refresh_seconds,
            self.app_settings.show_panel,self.http.isChecked())
        self.settings_store.write(self.indicator_settings,self.app_settings)

    def _settings(self):
        dialog=SettingsDialog(self.indicator_settings,self.app_settings,self)
        dialog.applied.connect(self._apply_settings)
        dialog.exec()

    def _apply_settings(self,indicator,application):
        self.indicator_settings=indicator
        self.app_settings=application
        self.settings_store.write(indicator,application)
        self.chart.set_settings(indicator)
        self.auto.blockSignals(True);self.auto.setChecked(application.auto_updates);self.auto.blockSignals(False)
        self.http.blockSignals(True);self.http.setChecked(application.allow_http);self.http.blockSignals(False)
        self.http_timer.setInterval(application.refresh_seconds*1000)
        self._auto_toggled(self.auto.isChecked())
        self._load(refresh=False)

    def _token_dialog(self):
        token, ok=QInputDialog.getText(self,"Finam Trade API",
            "Введите secret key (не access key). Он сохранится в системном хранилище Windows.",
            QLineEdit.EchoMode.Password)
        if not ok or not token:
            return
        try:
            save_secret(token)
        except Exception as exc:
            QMessageBox.warning(self,"Хранилище ключей",str(exc))
            return
        self.service.api._jwt=""
        self.status.setText("Токен сохранён в Windows Credential Manager; проверка подключения…")
        self._load(refresh=self.http.isChecked())
        self._start_stream()
        self._refresh_catalog()

    def _startup(self):
        if not read_secret():
            self.status.setText("Созданный токен Finam ещё не введён. Нажмите «Токен Finam».")
            self._indicator()
            return
        self._refresh_catalog()
        if self._pending_restore:
            self.status.setText("Finam: поиск сохранённого инструмента в каталоге…")
            return
        self._load(refresh=self.http.isChecked())
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
        self.ui_prefs.setValue("ticker",ticker)
        self.ui_prefs.setValue("mic",mic)
        self._load(refresh=False)
        self._start_stream()

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
            self.pending_load=request
            return
        self.loader=FinamLoadWorker(self.service,ticker,mic,request[2],
              self.indicator_settings,allow_http=self.http.isChecked(),
              refresh=refresh,events=request[4])
        self.loader.loaded.connect(self._loaded)
        self.loader.failed.connect(self._failed)
        self.loader.progress.connect(self._load_progress)
        self.loader.finished.connect(self._load_finished)
        self.status.setText("Finam: открытие "+ticker+"@"+mic+" · "+request[2])
        self._indicator()
        self.loader.start()

    def _loaded(self,main,parents,info):
        if self.closing or (info["symbol"],info["board"],info["tf"])!=self.current_view:
            return
        self.analysis,self.parents=main,parents
        self.displayed_view=self.current_view
        self.chart.instrument_header=info["symbol"]+"@"+info["board"]+" · Finam"
        self.chart.set_exchange(info["board"],info["symbol"])
        # Unknown step must never silently inherit the previous instrument's.
        self.chart.set_price_tick(info.get("price_tick_size"))
        self.chart.set_analysis(main,parents)
        self.chart.empty_text=""
        self.status.setText("Finam · "+info["tf"]+" · "+str(len(main.candles))+
            " свечей · "+info["warning"])
        log_snapshot(symbol=info["symbol"]+"@"+info["board"],
                     timeframe=info["tf"],asof=info.get("asof"),
                     delay_seconds=info.get("feed_delay_seconds"))
        delay=info.get("feed_delay_seconds",0)//60
        self.feed.setText("Finam · наблюдение "+
                          self._exchange_time(info["asof"],info["board"]) +
                          " · разрешённая задержка "+str(delay)+" мин · только чтение")
        self._indicator()

    def _current_load_signal(self):
        worker=self.sender()
        return worker is None or (worker.ticker,worker.mic,worker.timeframe)==self.current_view

    def _load_progress(self,message):
        if not self.closing and self._current_load_signal():
            self.status.setText(message)

    def _failed(self,reason):
        if self.closing or not self._current_load_signal():
            return
        symbol=(self.current_view[0]+"@"+self.current_view[1]
                if self.current_view else "")
        log_event("chart_load_failed",symbol=symbol,error=reason)
        self.status.setText("Finam: "+reason)
        if self.displayed_view != self.current_view:
            ticker,mic,tf=self.current_view
            self.chart.clear_analysis(f"Не удалось загрузить {ticker}@{mic} · {tf}\n{reason}")
            self.feed.setText(f"Finam · {ticker}@{mic} · {tf} · история не загружена")
        self._indicator()

    def _load_finished(self):
        thread=self.sender()
        if thread is self.loader:
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
            self._load(refresh=req[3],events=req[4])
        elif self.pending_bars:
            self.feed_timer.start()
        self._indicator()

    def _start_stream(self):
        if self.closing or not self.auto.isChecked() or not read_secret():
            self._indicator();return
        try:
            ticker,mic=symbol_name(self.symbol_edit.text(),self.mic.currentText()).split("@")
        except ValueError:
            return
        if self.stream is not None and (self.stream.ticker,self.stream.mic)==(ticker,mic):
            return
        self._stop_stream()
        self.stream= self.stream_factory(ticker,mic)
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
            old.stop()
            # Prevent a QThread deletion while transport closes.
            self._retiring=getattr(self,"_retiring",[])
            self._retiring.append(old)
            old.finished.connect(partial(self._retired,old))
        self.stream_ready=False
        self.pending_bars.clear()
        self.feed_timer.stop()

    def _retired(self,worker):
        if worker in self._retiring:
            self._retiring.remove(worker)
        worker.deleteLater()
        if self.closing:
            self._finish_close()

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
        if event.epoch > getattr(self,"stream_epoch",0):
            self.stream_epoch=event.epoch
            self.pending_bars.clear()
        if event.kind=="status":
            previous_state=self.stream_state
            self.stream_state=event.state
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
            q=event.quote
            last=q.get("last")
            if last is not None:
                instant=q.get("timestamp")
                stamp=(self._exchange_time(instant,event.board,seconds=True)
                       if instant is not None else "время сделки не указано")
                self.feed.setText(f"Finam WebSocket · {event.symbol}@{event.board} · цена {last} · "+stamp)
        elif event.kind=="candles":
            for c in event.candles:
                self.pending_bars[(event.timeframe,c.time)]=event.__class__(
                    event.kind,event.symbol,event.board,event.epoch,event.received_at,
                    timeframe=event.timeframe,candles=(c,))
            if not self.feed_timer.isActive():
                self.feed_timer.start()
        self._indicator()

    def _flush(self):
        if self.loader is not None or self.closing or not self.pending_bars:
            return
        events=tuple(self.pending_bars.values())
        self.pending_bars.clear()
        self._load(refresh=False,events=events)

    def _http_refresh(self):
        if (self.auto.isChecked() and self.http.isChecked() and
                self.loader is None and not self.closing):
            self._load(refresh=True)

    def _http_toggled(self,checked):
        self._save_settings()
        if checked:
            self._load(refresh=True)
        self._schedule_http()
        self._indicator()

    def _auto_toggled(self,checked):
        self._save_settings()
        if checked:self._start_stream()
        else:self._stop_stream()
        self._schedule_http()
        self._indicator()

    def _schedule_http(self):
        if self.auto.isChecked() and self.http.isChecked():
            if not self.http_timer.isActive():self.http_timer.start()
        else:
            self.http_timer.stop()

    def _indicator(self):
        if not hasattr(self,"channel"):
            return
        if self.stream_ready:
            label,color="FINAM · ПОТОК","#22553b"
        elif self.stream_state=="reconnecting":
            label,color="FINAM · ПЕРЕПОДКЛ.","#5b492b"
        elif self.stream is not None:
            label,color="FINAM · ПОДКЛ.","#52452e"
        elif not read_secret():
            label,color="FINAM · НЕТ ТОКЕНА","#4a3434"
        elif not self.auto.isChecked():
            label,color="ПОТОК · ПАУЗА","#383838"
        else:
            label,color="FINAM · НЕТ ПОТОКА","#493131"
        if self.loader is not None and self.loader.allow_http and self.loader.refresh:
            label,color="FINAM · HTTP","#2a415b"
        self.channel.setText(label)
        self.channel.setStyleSheet(f"background:{color};border:1px solid #555;"
                                   "border-radius:5px;color:#f0f0f0;padding:8px;font-weight:700")
        self.channel.setToolTip("Поставщик: Finam Trade API; HTTP для свечей: "+
            ("разрешён" if self.http.isChecked() else "запрещён")+
            ("\nПоследняя ошибка: "+self.stream_error if self.stream_error else ""))

    def _finish_close(self):
        if self.loader is None and self.stream is None and self.catalog_worker is None and not getattr(self,"_retiring",[]):
            QApplication.instance().quit()

    def closeEvent(self,event):
        self.closing=True
        self.feed_timer.stop()
        self.http_timer.stop()
        self._stop_stream()
        if self.loader is not None:
            self.loader.requestInterruption()
        if self.catalog_worker is not None:
            self.catalog_worker.requestInterruption()
        if self.loader is not None or self.catalog_worker is not None or getattr(self,"_retiring",[]):
            event.ignore()
            self.hide()
        else:
            event.accept()


def run():
    set_windows_application_id()
    app=QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("FlowDesk")
    app.setWindowIcon(application_icon())
    window=FinamWindow()
    window.show()
    return app.exec()
