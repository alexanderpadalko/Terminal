"""Right-side instrument list; HTTP refresh runs outside the GUI thread."""
from datetime import datetime, timedelta
import json
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from .market_data import MOSCOW
from .watchlist import WatchlistService


class ListWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, service):
        super().__init__()
        self.service = service

    def run(self):
        try:
            self.loaded.emit(self.service.refresh(self.isInterruptionRequested))
        except Exception as exc:
            self.failed.emit(str(exc))


class WatchlistController:
    def _build_watchlist(self):
        panel = QFrame()
        panel.setObjectName('instrumentList')
        panel.setMinimumWidth(235)
        panel.setMaximumWidth(310)
        layout = QVBoxLayout(panel)
        title = QLabel('Инструменты')
        title.setObjectName('section')
        layout.addWidget(title)
        self.list_search = QLineEdit()
        self.list_search.setPlaceholderText('Поиск по тикеру или названию')
        layout.addWidget(self.list_search)
        self.instrument_list = QTreeWidget()
        self.instrument_list.setHeaderHidden(True)
        self.instrument_list.setIndentation(12)
        self.instrument_list.setUniformRowHeights(True)
        self.instrument_list.itemClicked.connect(self._instrument_clicked)
        self.instrument_list.itemActivated.connect(self._instrument_clicked)
        self.list_search.textChanged.connect(self._filter_instruments)
        layout.addWidget(self.instrument_list, 1)
        self.list_status = QLabel('Загрузка списка…')
        self.list_status.setWordWrap(True)
        layout.addWidget(self.list_status)
        self.watchlist_panel = panel
        return panel

    def _init_lists(self, service):
        self.watchlist_service = service
        self.list_worker = None
        self.list_value = None
        self.list_retry_at = None
        self.list_timer = QTimer(self)
        self.list_timer.setInterval(60000)
        self.list_timer.timeout.connect(self._refresh_lists)
        value = service.cached() if service else None
        if value is None:
            try:
                value = WatchlistService.validate(json.loads((Path(__file__).parent / 'data' / 'watchlist_seed.json').read_text(encoding='utf-8')))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if value:
            self._lists_loaded(value)
        if service:
            self.list_timer.start()
            QTimer.singleShot(0, self._refresh_lists)

    def _refresh_lists(self):
        if (not self.watchlist_service or self.list_worker or self.closing_after_load or
                not self.watchlist_service.due(self.list_value)):
            return
        if self.list_retry_at and datetime.now(MOSCOW) < self.list_retry_at:
            return
        self.list_status.setText('Обновление списка…')
        worker = ListWorker(self.watchlist_service)
        self.list_worker = worker
        worker.loaded.connect(self._lists_loaded)
        worker.failed.connect(self._lists_failed)
        worker.finished.connect(self._lists_finished)
        worker.start()

    def _lists_loaded(self, value):
        if self.closing_after_load:
            return
        self.list_value = value
        self.list_retry_at = None
        self.instrument_list.clear()
        groups = {kind: QTreeWidgetItem(self.instrument_list, [name]) for kind, name in
                  (('stocks', 'Акции · 40'), ('futures', 'Фьючерсы · 10'))}
        self.list_items = {}
        today = datetime.now(MOSCOW).date()
        for row in value['instruments']:
            group = groups['stocks' if row['kind'] == 'stock' else 'futures']
            item = QTreeWidgetItem(group, [f"{row['symbol']}  ·  {row['name']}"])
            item.setData(0, Qt.ItemDataRole.UserRole, row)
            tooltip = row.get('tv_symbol', row['symbol'])
            if row.get('expiry'):
                remaining = (datetime.fromisoformat(row['expiry']).date() - today).days
                tooltip += f"\nОкончание торгов: {row['expiry']}"
                if remaining <= 10:
                    item.setText(0, item.text(0) + f' · {remaining} дн.')
                    tooltip += f"\nСледующая серия: {row.get('next_symbol') or 'не подтверждена'} ({row.get('next_expiry') or '—'})"
                    tooltip += f"\nПереход: {row['expiry']} 06:00 МСК"
            item.setToolTip(0, tooltip)
            self.list_items[(row['symbol'], row['board'])] = item
        self.instrument_list.expandAll()
        self._filter_instruments(self.list_search.text())
        self._highlight_instrument()
        self.list_status.setText(f"Список от {value['slot']} · 06:00 МСК")
        window = value.get('window', [])
        self.list_status.setToolTip((f'Оборот за {window[0]} — {window[-1]}: 20 завершённых сессий.\n' if window else 'Сохранённый список; требуется обновление.\n') +
            'Обыкновенные и привилегированные акции отдельно. Фьючерсные активы фиксированы; меняется только серия при экспирации.')

    def _lists_failed(self, error):
        self.list_retry_at = datetime.now(MOSCOW) + timedelta(minutes=10)
        stamp = self.list_value.get('slot', '—') if self.list_value else '—'
        self.list_status.setText(f'Список от {stamp} · обновление недоступно')
        self.list_status.setToolTip(error)

    def _lists_finished(self):
        worker = self.sender()
        if worker is self.list_worker:
            self.list_worker = None
        worker.deleteLater()
        if self.closing_after_load:
            self.close()

    def _filter_instruments(self, query):
        query = query.strip().casefold()
        root = self.instrument_list.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            visible = False
            for j in range(group.childCount()):
                item = group.child(j)
                match = query in item.text(0).casefold() or query in item.toolTip(0).casefold()
                item.setHidden(not match)
                visible |= match
            group.setHidden(not visible)

    def _highlight_instrument(self):
        context = self.loaded_market or self.desired_market
        if context:
            item = getattr(self, 'list_items', {}).get(context[:2])
            if item:
                self.instrument_list.setCurrentItem(item)

    def _instrument_clicked(self, item, column=0):
        row = item.data(0, Qt.ItemDataRole.UserRole)
        if not row:
            return
        context = (row['symbol'], row['board'], self.tf.currentText())
        if context == self.desired_market and (self.worker is not None or context == self.loaded_market):
            return
        self.symbol.setText(row['symbol'])
        self.board.setCurrentText(row['board'])
        self._request_market(refresh=False)
