"""Qt Widgets desktop UI with an interactive candlestick chart.

The code contains no order/trade APIs. All calculations are explicitly marked
as experimental until the user-provided TradingView CSV tests establish parity.
"""
from __future__ import annotations

import bisect
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPaintEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .engine import Analysis, analyze
from .market_data import Candle, MOSCOW, TIMEFRAMES, download_moex, read_csv, infer_timeframe

BACKGROUND = QColor("#0c131e")
GRID = QColor("#202b3b")
TEXT = QColor("#b7c3d5")
UP = QColor("#20c997")
DOWN = QColor("#f46b82")
ACCENT = QColor("#66a8ff")


def short_number(x: float) -> str:
    return f"{x:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


def colored(text: str, color: str) -> str:
    return f'<span style="color:{color};font-weight:700">{text}</span>'


def demo_data() -> list[Candle]:
    """Deterministic offline demo clearly labelled SYNTHETIC; never sold as MOEX."""
    rng = random.Random(11072026)
    start = datetime(2026, 10, 1, 10, 0, tzinfo=MOSCOW)
    last = 100.0
    result: list[Candle] = []
    for i in range(500):
        drift = (0.28 if (i // 65) % 2 == 0 else -0.30) + math.sin(i / 12) * 0.22
        nxt = max(1, last + drift + rng.gauss(0, 0.37))
        hi = max(last, nxt) + rng.uniform(0.1, 0.9)
        lo = min(last, nxt) - rng.uniform(0.1, 0.9)
        result.append(Candle(start + timedelta(hours=i), last, hi, lo, nxt, rng.uniform(100, 2000)))
        last = nxt
    return result


class MarketWorker(QThread):
    loaded = Signal(object, object, str)
    failed = Signal(str)

    def __init__(self, symbol: str, tf: str, board: str):
        super().__init__()
        self.symbol, self.tf, self.board = symbol, tf, board

    def run(self):
        try:
            bars = download_moex(self.symbol, self.tf, limit=3000, board=self.board)
            higher: dict[str, Analysis] = {}
            order = ["M1", "M5", "H1", "D1", "W1", "MN1"]
            for tf in ["H1", "D1", "W1", "MN1"]:
                if order.index(tf) > order.index(self.tf):
                    # A separate H1 series is REQUIRED for M1/M5 flow; DO NOT
                    # silently fallback to local M1/M5 FlowEngine if unavailable.
                    try:
                        older = download_moex(self.symbol, tf, limit=3000 if tf == "H1" else 1600, board=self.board)
                        higher[tf] = analyze(older, tf)
                    except Exception:
                        # Lower candle chart and FVG remain available, but if
                        # H1 failed, flow will be marked unknown.
                        pass
            main = analyze(bars, self.tf,
                           higher_h1=higher.get("H1"), asof=datetime.now(timezone.utc))
            self.loaded.emit(main, higher, self.symbol)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class CandleChart(QWidget):
    crosshair = Signal(int)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setMinimumSize(600, 430)
        self.analysis: Analysis | None = None
        self.parents: dict[str, Analysis] = {}
        self.show_structure = True
        self.show_fvg = True
        self.show_flow = True
        self.show_previous = True
        self.view_count = 125
        self.offset = 0
        self.cursor_x: float | None = None
        self.cursor_y: float | None = None
        self.drag_origin = None
        self.drag_offset = 0
        self.x0 = 65
        self.x1 = 100
        self.top = 26
        self.bottom = 80

    def set_analysis(self, analysis: Analysis, parents: dict[str, Analysis] | None = None):
        self.analysis = analysis
        self.parents = parents or {}
        self.offset = 0
        self.update()

    def window(self):
        n = len(self.analysis.candles) if self.analysis else 0
        end = max(0, n - min(self.offset, max(0, n - 8)))
        start = max(0, end - self.view_count)
        return start, end

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta > 0:
            self.view_count = max(25, int(self.view_count * 0.82))
        elif delta < 0:
            self.view_count = min(500, int(self.view_count * 1.23) + 1)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_origin = event.position().x()
            self.drag_offset = self.offset

    def mouseReleaseEvent(self, event):
        self.drag_origin = None

    def mouseMoveEvent(self, event):
        self.cursor_x, self.cursor_y = event.position().x(), event.position().y()
        if self.drag_origin is not None and self.analysis:
            dx = self.cursor_x - self.drag_origin
            width = max(1, self.width() - 145)
            bars_drag = int(dx * self.view_count / width)
            self.offset = max(0, min(len(self.analysis.candles) - 8, self.drag_offset + bars_drag))
        if self.analysis:
            first, last = self.window()
            plot_width = self.width() - 155
            index = first + int((self.cursor_x - 66) / max(1, plot_width) * max(1, last - first))
            if first <= index < last:
                self.crosshair.emit(index)
        self.update()

    def leaveEvent(self, event):
        self.cursor_x = self.cursor_y = None
        self.update()

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BACKGROUND)
        if self.analysis is None or not self.analysis.candles:
            p.setPen(TEXT)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Загрузите CSV или инструмент MOEX")
            return
        candles = self.analysis.candles
        first, last = self.window()
        if last <= first:
            return
        self.x0, self.x1 = 64, max(100, self.width() - 88)
        self.top, self.bottom = 30, max(130, self.height() - 72)
        count = last - first
        span = max(1, self.x1 - self.x0)
        stride = span / max(1, count)
        def x(index: int):
            return self.x0 + (index - first + 0.5) * stride
        visible = candles[first:last]
        lowest = min(b.low for b in visible)
        highest = max(b.high for b in visible)
        if self.show_fvg:
            for z in self.analysis.fvg[-300:]:
                if z.created >= first and z.start <= last and (z.broken is None or z.broken >= first):
                    lowest, highest = min(lowest, z.bottom), max(highest, z.top)
        if self.show_flow and self.analysis.flows:
            st = self.analysis.flows[last - 1]
            if st.lo is not None:
                lowest, highest = min(lowest, st.lo), max(highest, st.hi)
        gap = max(0.001, (highest - lowest) * 0.075)
        lowest -= gap
        highest += gap
        def y(value: float):
            return self.bottom - (value - lowest) / max(1e-8, highest - lowest) * (self.bottom - self.top)
        p.setFont(QFont("Segoe UI", 9))
        for j in range(7):
            value = lowest + (highest - lowest) * j / 6
            Y = y(value)
            p.setPen(QPen(GRID, 1))
            p.drawLine(QPointF(self.x0, Y), QPointF(self.x1, Y))
            p.setPen(TEXT)
            p.drawText(QRectF(self.x1 + 8, Y - 11, 80, 20), 0, short_number(value))
        for j in range(7):
            idx = first + int(j * (count - 1) / 6)
            p.setPen(QPen(GRID, 1))
            p.drawLine(QPointF(x(idx), self.top), QPointF(x(idx), self.bottom))
            p.setPen(TEXT)
            p.drawText(QRectF(x(idx) - 52, self.bottom + 10, 104, 20), int(Qt.AlignmentFlag.AlignCenter), candles[idx].time.strftime("%d.%m %H:%M" if self.analysis.timeframe in ("M1", "M5", "H1") else "%d.%m.%Y"))
        # FVG backgrounds and edges: local zones preserve break candle bounds.
        if self.show_fvg:
            local_zones = self.analysis.fvg[-300:]
            for z in local_zones:
                if z.created < first - 450 or z.start >= last:
                    continue
                right = min(last - 1, z.broken if z.broken is not None else last - 1)
                left = max(first, z.start)
                if right < left:
                    continue
                self._draw_zone(p, x(left) - stride / 2, x(right) + stride / 2, y(z.top), y(z.bottom), z.side, z.broken is not None, z.source_tf, stride)
            for parent_tf, parent in self.parents.items():
                if not parent.candles:
                    continue
                times = [b.time.timestamp() for b in candles]
                for z in parent.fvg[-14:]:
                    # Closed-source restriction: exclude the latest parent candle,
                    # which may still be developing in ISS.
                    if z.created >= len(parent.candles) - 1:
                        continue
                    left_ts = parent.candles[z.start].time.timestamp()
                    right_ts = parent.candles[z.broken].time.timestamp() if z.broken is not None else candles[-1].time.timestamp()
                    a = bisect.bisect_left(times, left_ts)
                    b = bisect.bisect_right(times, right_ts) - 1
                    if b < first or a >= last or b < a:
                        continue
                    left, right = max(first, a), min(last - 1, b)
                    self._draw_zone(p, x(left) - stride / 2, x(right) + stride / 2, y(z.top), y(z.bottom), z.side, z.broken is not None, parent_tf, stride, parent=True)
        # Structure with confirmed segments and provisional outside legs.
        if self.show_structure:
            for seg in self.analysis.segments:
                if seg.a is None or seg.b is None or seg.b < first or seg.a > last:
                    continue
                a, b = max(seg.a, first), min(seg.b, last - 1)
                if a > b:
                    continue
                if seg.a == seg.b:
                    continue
                ratio_a = (a - seg.a) / (seg.b - seg.a)
                ratio_b = (b - seg.a) / (seg.b - seg.a)
                ya = seg.pa + (seg.pb - seg.pa) * ratio_a
                yb = seg.pa + (seg.pb - seg.pa) * ratio_b
                pen = QPen(UP if seg.side == 1 else DOWN, 2.2)
                if not seg.confirmed:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(x(a), y(ya)), QPointF(x(b), y(yb)))
        # Candles: draw over FVG and structure with high-contrast wicks/bodies.
        for i in range(first, last):
            bar = candles[i]
            color = UP if bar.close >= bar.open else DOWN
            center = x(i)
            p.setPen(QPen(color, 1.2))
            p.drawLine(QPointF(center, y(bar.high)), QPointF(center, y(bar.low)))
            body_w = max(2.0, min(18.0, stride * 0.62))
            a, b = y(bar.open), y(bar.close)
            p.fillRect(QRectF(center - body_w / 2, min(a, b), body_w, max(1.3, abs(b - a))), color)
        # Current and previous flow 0, 0.4, 0.5, 0.6, 1.0.
        if self.show_flow and self.analysis.flows:
            st = self.analysis.flows[last - 1]
            if st.side and st.lo is not None and st.hi is not None:
                starts = max(first, st.start or first)
                for t, color in zip((0.0, 0.4, 0.5, 0.6, 1.0), (QColor("#4da95c"), QColor("#4da95c"), QColor("#afafb8"), QColor("#dc6574"), QColor("#dc6574"))):
                    price = st.lo + (st.hi - st.lo) * t
                    p.setPen(QPen(color, 1.5))
                    Y = y(price)
                    p.drawLine(QPointF(x(starts), Y), QPointF(self.x1, Y))
                    p.drawText(QRectF(self.x1 - 49, Y - 15, 50, 14), int(Qt.AlignmentFlag.AlignRight), str(t))
            if self.show_previous and st.previous:
                old_start, old_end, old_lo, old_hi, _ = st.previous
                if old_lo is not None and old_hi is not None:
                    for r in (0.0, 0.4, 0.5, 0.6, 1.0):
                        Y = y(old_lo + (old_hi - old_lo) * r)
                        pen = QPen(QColor(85, 111, 144, 110), 1)
                        pen.setStyle(Qt.PenStyle.DashLine)
                        p.setPen(pen)
                        p.drawLine(QPointF(x(max(first, old_start)), Y), QPointF(x(min(last - 1, max(first, old_end))), Y))
        # Price marker and floating header.
        latest = candles[last - 1]
        color = UP if latest.close >= latest.open else DOWN
        p.fillRect(QRectF(self.x1 + 3, y(latest.close) - 10, 82, 20), color)
        p.setPen(QColor("#09151d"))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(QRectF(self.x1 + 5, y(latest.close) - 10, 80, 20), int(Qt.AlignmentFlag.AlignCenter), short_number(latest.close))
        p.setPen(TEXT)
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(QRectF(self.x0, 0, max(150, self.width() - 120), 23), 0,
                   f"{self.analysis.timeframe}   O {short_number(latest.open)}   H {short_number(latest.high)}   L {short_number(latest.low)}   C {short_number(latest.close)}")
        if self.cursor_x is not None and self.x0 <= self.cursor_x <= self.x1 and self.top <= self.cursor_y <= self.bottom:
            pen = QPen(QColor("#8395a8"), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(self.cursor_x, self.top), QPointF(self.cursor_x, self.bottom))
            p.drawLine(QPointF(self.x0, self.cursor_y), QPointF(self.x1, self.cursor_y))
        p.setPen(QColor("#8294a9"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(self.x0, self.height() - 32, self.width() - 170, 20), 0,
                   "Прокрутка: масштаб · ЛКМ+перетаскивание: история · Диагностика не сверена с Pine")
        p.end()

    def _draw_zone(self, p, x0, x1, y0, y1, side, broken, tf, stride, parent=False):
        if x1 < x0:
            return
        low_y, high_y = min(y0, y1), max(y0, y1)
        height = max(1.5, high_y - low_y)
        if height < 1.5:
            return
        if broken:
            bg, edge = QColor(243, 169, 45, 40), QColor(241, 173, 70, 130)
        elif side == 1:
            bg, edge = QColor(24, 196, 145, 38 if not parent else 28), QColor(19, 148, 123, 135)
        else:
            bg, edge = QColor(221, 70, 100, 38 if not parent else 28), QColor(204, 74, 95, 135)
        p.fillRect(QRectF(x0, low_y, x1 - x0, height), bg)
        p.setPen(QPen(edge, 1))
        p.drawRect(QRectF(x0, low_y, x1 - x0, height))
        p.setPen(QPen(edge, 0.8, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(x0, (y0 + y1) / 2), QPointF(x1, (y0 + y1) / 2))
        if x1 - x0 > 65 and height >= 20:
            p.setPen(TEXT)
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(x0 + 3, low_y + 2, min(98, x1 - x0 - 4), 14), 0, f"{tf} {'↑' if side == 1 else '↓'} FVG")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FlowDesk {__version__} — Структура и FVG · Windows")
        self.setMinimumSize(1060, 690)
        self.resize(1420, 830)
        self.worker: MarketWorker | None = None
        self.source = "DEMO (синтетические свечи)"
        self.analysis: Analysis | None = None
        self.parents: dict[str, Analysis] = {}
        self.chart = CandleChart()
        self._build_ui()
        self._show_data(analyze(demo_data(), "H1"), {}, self.source)

    def _build_ui(self):
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)
        header = QHBoxLayout()
        brand = QLabel("◈  FLOWDESK")
        brand.setObjectName("brand")
        header.addWidget(brand)
        header.addSpacing(12)
        header.addWidget(QLabel("Режим:"))
        self.board = QComboBox()
        self.board.addItems(["TQBR", "TQTF", "TQTD", "SPEQ", "TQOB"])
        self.board.setToolTip("Режим торгов MOEX (для акций обычно TQBR)")
        header.addWidget(self.board)
        self.symbol = QLineEdit("SBER")
        self.symbol.setFixedWidth(110)
        self.symbol.setPlaceholderText("Тикер")
        header.addWidget(self.symbol)
        self.tf = QComboBox()
        self.tf.addItems(list(TIMEFRAMES))
        self.tf.setCurrentText("H1")
        header.addWidget(self.tf)
        self.load_button = QPushButton("Обновить MOEX")
        self.load_button.clicked.connect(self.load_moex)
        header.addWidget(self.load_button)
        self.csv_button = QPushButton("Открыть CSV")
        self.csv_button.clicked.connect(self.open_csv)
        header.addWidget(self.csv_button)
        self.h1_button = QPushButton("Добавить H1 CSV")
        self.h1_button.setToolTip("На M1/M5 импорт отдельной истории H1 для потока. Без неё поток не рассчитывается.")
        self.h1_button.clicked.connect(self.open_h1_csv)
        header.addWidget(self.h1_button)
        self.export_button = QPushButton("Экспорт диагностики")
        self.export_button.clicked.connect(self.export_diagnostics)
        header.addWidget(self.export_button)
        header.addStretch()
        layout.addLayout(header)
        self.banner = QLabel("ПРЕДВАРИТЕЛЬНЫЕ РАСЧЁТЫ • совпадение с TradingView не подтверждено • без выставления заявок")
        self.banner.setObjectName("warning")
        layout.addWidget(self.banner)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.chart)
        right = QWidget()
        right.setMinimumWidth(280)
        right.setMaximumWidth(360)
        panel = QVBoxLayout(right)
        panel.setContentsMargins(8, 0, 0, 0)
        panel.setSpacing(8)
        info_title = QLabel("Состояние инструмента")
        info_title.setObjectName("section")
        panel.addWidget(info_title)
        self.instrument_label = QLabel()
        self.instrument_label.setWordWrap(True)
        panel.addWidget(self.instrument_label)
        self.price_label = QLabel()
        self.price_label.setObjectName("bigPrice")
        panel.addWidget(self.price_label)
        self.flow_label = QLabel()
        self.flow_label.setWordWrap(True)
        panel.addWidget(self.flow_label)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        panel.addWidget(self.state_label)
        options = QLabel("Отображение")
        options.setObjectName("section")
        panel.addWidget(options)
        for label, prop in [
            ("Структурная ломаная", "show_structure"),
            ("FVG и зоны старших ТФ", "show_fvg"),
            ("Уровни потока 0–1", "show_flow"),
            ("Предыдущий поток", "show_previous"),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.toggled.connect(lambda value, key=prop: self._toggle(key, value))
            panel.addWidget(cb)
        zone_title = QLabel("Последние FVG")
        zone_title.setObjectName("section")
        panel.addWidget(zone_title)
        self.zones_table = QTableWidget(0, 3)
        self.zones_table.setHorizontalHeaderLabels(["ТФ / тип", "Низ", "Верх"])
        self.zones_table.verticalHeader().setVisible(False)
        self.zones_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.zones_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.zones_table.setMaximumHeight(198)
        self.zones_table.horizontalHeader().setStretchLastSection(True)
        self.zones_table.setColumnWidth(0, 108)
        self.zones_table.setColumnWidth(1, 80)
        panel.addWidget(self.zones_table)
        event_title = QLabel("Последние события")
        event_title.setObjectName("section")
        panel.addWidget(event_title)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMinimumHeight(100)
        panel.addWidget(self.events, 1)
        self.hover_info = QLabel("Наведите курсор на свечу")
        self.hover_info.setWordWrap(True)
        panel.addWidget(self.hover_info)
        splitter.addWidget(right)
        splitter.setSizes([1030, 320])
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setObjectName("status")
        layout.addWidget(self.status)
        self.chart.crosshair.connect(self._hover)
        self._style()

    def _style(self):
        self.setStyleSheet("""
        QMainWindow, QWidget { background: #101925; color: #d5e1ef; font-family: 'Segoe UI'; font-size: 12px; }
        #brand { font-size: 18px; font-weight: 800; color: #86b8ff; letter-spacing: 1px; }
        #section { font-weight: 700; font-size: 13px; color: #9bb2c9; margin-top: 6px; }
        #warning { background: #302822; color: #eeb985; border: 1px solid #71533c; padding: 8px; border-radius: 5px; }
        #bigPrice { font-size: 24px; font-weight: 700; color: #f1f6ff; }
        #status { color: #9aaec1; font-size: 11px; }
        QLineEdit, QComboBox, QPushButton { background: #1d2b3d; color: #e5edf7; border: 1px solid #34465c; padding: 7px; border-radius: 4px; }
        QPushButton:hover { background: #294565; border: 1px solid #4f81b6; }
        QPushButton:disabled { color: #798ba1; background: #1b2431; }
        QPlainTextEdit, QTableWidget { background: #172231; color: #d6dfec; border: 1px solid #2a3a50; gridline-color: #2a3a50; }
        QHeaderView::section { background: #1b2b3d; color: #c6d6e8; padding: 5px; border: 0px; }
        QCheckBox { spacing: 9px; padding: 3px; }
        QSplitter::handle { background: #25384d; width: 3px; }
        """)

    def _toggle(self, key: str, value: bool):
        setattr(self.chart, key, value)
        self.chart.update()

    def load_moex(self):
        if self.worker and self.worker.isRunning():
            return
        symbol = self.symbol.text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Тикер", "Введите тикер, например SBER")
            return
        self.load_button.setEnabled(False)
        self.status.setText(f"Запрос данных MOEX ISS: {symbol} · {self.tf.currentText()} (исторические свечи, возможна задержка)")
        self.worker = MarketWorker(symbol, self.tf.currentText(), self.board.currentText())
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(lambda: self.load_button.setEnabled(True))
        self.worker.start()

    def _loaded(self, analysis: Analysis, higher: dict, symbol: str):
        self._show_data(analysis, higher, f"MOEX: {symbol} / {self.board.currentText()}")

    def _failed(self, error: str):
        self.status.setText(f"Ошибка загрузки: {error}")
        QMessageBox.warning(self, "MOEX ISS", error)

    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать экспорт TradingView", "", "CSV (*.csv);;Все файлы (*.*)")
        if not path:
            return
        try:
            candles = read_csv(path)
            detected = infer_timeframe(candles)
            tf = detected or self.tf.currentText()
            self.tf.setCurrentText(tf)
            # Never apply a prior H1 CSV from another instrument to a newly
            # opened lower-TF CSV. User explicitly imports the matching H1.
            analysis = analyze(candles, tf)
            self._show_data(analysis, {}, f"CSV: {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка CSV", str(exc))

    def open_h1_csv(self):
        if (not self.analysis or self.analysis.timeframe not in ("M1", "M5")
                or not self.source.startswith("CSV:")):
            QMessageBox.information(self, "Источник H1", "Сначала откройте CSV M1/M5 того же инструмента. Для MOEX H1 загружается автоматически.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "CSV H1 того же инструмента", "", "CSV (*.csv);;Все файлы (*.*)")
        if not path:
            return
        try:
            candles = read_csv(path)
            inferred = infer_timeframe(candles)
            if inferred != "H1":
                raise ValueError(f"Ожидается часовой CSV H1. Определён: {inferred or 'не удалось определить'}")
            parent = analyze(candles, "H1")
            lower = self.analysis
            if (candles[-1].time < lower.candles[0].time or
                    candles[0].time > lower.candles[-1].time):
                raise ValueError("Интервалы H1 и младшего CSV не пересекаются. Проверьте тикер и даты.")
            updated = analyze(lower.candles, lower.timeframe, higher_h1=parent)
            updated.flow_status = "H1_CSV_PROVISIONAL"
            updated.flow_source_note = f"H1 из CSV {Path(path).name}; тот же тикер требуется проверить вручную"
            self._show_data(updated, {"H1": parent}, self.source)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка импорта H1", str(exc))

    def _show_data(self, analysis: Analysis, parents: dict, source: str):
        self.source = source
        self.analysis = analysis
        self.parents = parents
        self.chart.set_analysis(analysis, parents)
        state = analysis.flows[-1]
        price = analysis.candles[-1].close
        flow_origin = ("H1, только закрытые свечи" if analysis.timeframe in ("M1", "M5") else
                       f"{analysis.timeframe}, локальный предварительный расчёт")
        if analysis.flow_status == "H1_UNAVAILABLE":
            flow_origin = "H1 недоступен — поток отключён"
        self.instrument_label.setText(
            f"<b>{source}</b> · {analysis.timeframe}<br>"
            f"Последняя свеча: {analysis.candles[-1].time.strftime('%d.%m.%Y %H:%M %Z')}<br>"
            f"Источник потока: <b>{flow_origin}</b>"
        )
        self.price_label.setText(short_number(price))
        if state.side == 1:
            caption, color = "ЛОНГ", "#20c997"
        elif state.side == -1:
            caption, color = "ШОРТ", "#f46b82"
        else:
            caption, color = "ОЖИДАНИЕ", "#edb974"
        self.flow_label.setText(f"Поток: {colored(caption, color)} · <b>алгоритм не сверен с TradingView</b>")
        self.state_label.setText(f"Диапазон: {short_number(state.lo) if state.lo is not None else '—'} — {short_number(state.hi) if state.hi is not None else '—'}<br>Рабочий FVG: {'есть' if state.guard and not state.guard_broken else 'нет'}<br>Пара IDM: {self._pair_text(state)}")
        events = [(analysis.candles[s.i].time.strftime("%d.%m %H:%M"), s.diagnosis) for s in analysis.flows if s.rule in (2, 3)]
        self.events.setPlainText("\n".join(f"{t}  {reason}" for t, reason in events[-18:][::-1]) or "Нет подтверждённых по времени событий H1 №2/№3 (или ряд H1 отсутствует)")
        zones = analysis.fvg[-17:][::-1]
        self.zones_table.setRowCount(len(zones))
        for i, z in enumerate(zones):
            values = [f"{z.source_tf} {'↑' if z.side == 1 else '↓'} {'×' if not z.active else ''}", short_number(z.bottom), short_number(z.top)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setForeground(UP if z.side == 1 else DOWN)
                self.zones_table.setItem(i, col, item)
        self.status.setText(f"{source} · {len(analysis.candles)} свечей · {len(analysis.fvg)} FVG · {len(analysis.segments)} отрезков · {analysis.flow_source_note} · НЕ сверено с Pine")

    def _pair_text(self, state) -> str:
        pair = state.liquidity
        if pair and pair.high is not None and pair.low is not None:
            return f"{short_number(pair.low)} / {short_number(pair.high)}"
        return "—"

    def _hover(self, i: int):
        if not self.analysis or i < 0 or i >= len(self.analysis.candles):
            return
        bar = self.analysis.candles[i]
        flow = self.analysis.flows[i]
        dirtext = "LONG" if flow.side == 1 else "SHORT" if flow.side == -1 else "WAIT"
        h1_info = (f" · H1 закрыта {flow.source_confirmed_at.strftime('%d.%m %H:%M')}" if flow.source_confirmed_at else "")
        self.hover_info.setText(f"{bar.time.strftime('%d.%m.%Y %H:%M')} · O {short_number(bar.open)} · H {short_number(bar.high)} · L {short_number(bar.low)} · C {short_number(bar.close)} · {dirtext}{h1_info}")

    def export_diagnostics(self):
        if not self.analysis:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт диагностики", "flowdesk_diagnostic.json", "JSON (*.json)")
        if not path:
            return
        try:
            a = self.analysis
            dump = {
                "product": "FlowDesk", "version": __version__, "parity": "UNVERIFIED",
                "source": self.source, "timeframe": a.timeframe, "no_orders": True,
                "flow_source_tf": a.flow_source_tf,
                "flow_status": a.flow_status,
                "flow_source_note": a.flow_source_note,
                "caveats": ["Rule 1 MTF confirmation not implemented", "Rule 2/3 not verified against TradingView", "Quotes may be delayed", "MOEX candle alignment and sessions not parity-verified"],
                "bars": len(a.candles),
                "candles": [{"time": c.time.isoformat(), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in a.candles],
                "flows": [{"time": c.time.isoformat(), "side": f.side, "lo": f.lo, "hi": f.hi, "start": f.start, "rule": f.rule, "diagnosis": f.diagnosis,
                           "source_tf": f.source_tf or a.flow_source_tf,
                           "source_h1_open": f.source_bar_time.isoformat() if f.source_bar_time else None,
                           "source_h1_closed_at": f.source_confirmed_at.isoformat() if f.source_confirmed_at else None}
                          for c, f in zip(a.candles, a.flows)],
                "fvg": [{"time": a.candles[z.created].time.isoformat(), "start": z.start, "side": z.side, "top": z.top, "bottom": z.bottom, "balanced": z.balanced, "broken_at": z.broken} for z in a.fvg],
                "h1_reference": (None if "H1" not in self.parents else {
                    "source": "MOEX separate H1" if self.source.startswith("MOEX:") else "User CSV H1",
                    "bars": len(self.parents["H1"].candles),
                    "events": [{"time": self.parents["H1"].candles[f.i].time.isoformat(), "side": f.side,
                                "lo": f.lo, "hi": f.hi, "rule": f.rule}
                               for f in self.parents["H1"].flows if f.rule in (1, 2, 3)]
                }),
            }
            Path(path).write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
            self.status.setText(f"Экспортировано: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка экспорта", str(exc))


def run():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("FlowDesk")
    window = MainWindow()
    window.show()
    return app.exec()
