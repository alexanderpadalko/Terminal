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
    QInputDialog, QButtonGroup, QToolButton,
)

from . import __version__
from .chart_view import ChartView, inside_mother_bars
from .market_cache import MarketService
from .engine import Analysis, analyze
from .pipeline import analyze_timeframes, source_coverage
from .liquidity import serialize_audit
from .market_data import (Candle, MOSCOW, TIMEFRAMES, load_moex_session_bundle,
                          exclude_opening_auction, load_session_overrides,
                          read_csv, infer_timeframe, candle_close_time, serialize_candle)

BACKGROUND = QColor("#0c131e")
GRID = QColor("#202b3b")
TEXT = QColor("#b7c3d5")
UP = QColor("#20c997")
DOWN = QColor("#f46b82")
ACCENT = QColor("#66a8ff")

# TV mode is the default; the previous dark palette remains available.
# Only paint/UI colors are defined here. Market data and indicator calculations
# must not depend on the selected visual theme.
CHART_PALETTES = {
    "tv": {
        "background": QColor("#898989"),
        "grid": QColor(160, 160, 160, 45),
        "text": QColor("#292b2f"),
        "structure_up": QColor("#008476"),
        "structure_down": QColor("#b21f34"),
        "candle_up": QColor("#ffffff"),
        "candle_down": QColor("#111111"),
        "candle_up_outline": QColor("#f8f8f8"),
        "candle_down_outline": QColor("#141414"),
        "wick": QColor("#353535"),
        "flow_lines": (QColor("#205b28"), QColor("#205b28"),
                       QColor("#666666"), QColor("#c32238"), QColor("#c32238")),
        "previous": QColor(62, 62, 62, 105),
        "crosshair": QColor("#434343"),
        "tooltip_bg": QColor("#343434"),
        "tooltip_text": QColor("#ffffff"),
        "price_marker": QColor("#161616"),
        "price_marker_text": QColor("#ffffff"),
    },
    "dark": {
        "background": BACKGROUND,
        "grid": GRID,
        "text": TEXT,
        "structure_up": UP,
        "structure_down": DOWN,
        "candle_up": UP,
        "candle_down": DOWN,
        "candle_up_outline": UP,
        "candle_down_outline": DOWN,
        "wick": None,
        "flow_lines": (QColor("#4da95c"), QColor("#4da95c"),
                       QColor("#afafb8"), QColor("#dc6574"), QColor("#dc6574")),
        "previous": QColor(85, 111, 144, 110),
        "crosshair": QColor("#8395a8"),
        "tooltip_bg": QColor("#34465c"),
        "tooltip_text": TEXT,
        "price_marker": None,
        "price_marker_text": QColor("#09151d"),
    },
}


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


class TimeframeBar(QWidget):
    """Exclusive chart interval buttons with a stable selection signal."""
    currentTextChanged = Signal(str)
    LABELS = (("M1", "1м"), ("M5", "5м"), ("H1", "1ч"),
              ("H4", "4ч"), ("D1", "Д"), ("W1", "Н"), ("MN1", "М"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("timeframeBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(3)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        self._current = "H1"
        for tf, label in self.LABELS:
            button = QToolButton(self)
            button.setText(label)
            button.setCheckable(True)
            button.setFixedSize(37, 42)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip({"M1": "1 минута", "M5": "5 минут", "H1": "1 час",
                               "H4": "4 часа", "D1": "День", "W1": "Неделя", "MN1": "Месяц"}[tf])
            button.setAccessibleName(button.toolTip())
            button.clicked.connect(lambda checked=False, value=tf: self.setCurrentText(value))
            self.group.addButton(button)
            self.buttons[tf] = button
            layout.addWidget(button)
        self.buttons[self._current].setChecked(True)
        self.set_theme("tv")

    def set_theme(self, theme: str):
        if theme == "tv":
            self.setStyleSheet("""
                QWidget#timeframeBar { background: #dedede; border-radius: 6px; }
                QWidget#timeframeBar QToolButton {
                    background: transparent; color: #393939; border: none;
                    border-radius: 6px; padding: 0; font-size: 16px;
                }
                QWidget#timeframeBar QToolButton:hover { background: #cccccc; }
                QWidget#timeframeBar QToolButton:checked {
                    background: #ffffff; color: #111111; font-weight: 700;
                    border: 1px solid #bcbcbc;
                }
                QWidget#timeframeBar QToolButton:focus { border: 1px solid #848484; }
                QWidget#timeframeBar QToolButton:disabled { color: #aaaaaa; }
            """)
        else:
            self.setStyleSheet("""
                QWidget#timeframeBar { background: #101010; border-radius: 8px; }
                QWidget#timeframeBar QToolButton {
                    background: transparent; color: #dfdfdf; border: none;
                    border-radius: 10px; padding: 0; font-size: 18px;
                }
                QWidget#timeframeBar QToolButton:hover { background: #262626; }
                QWidget#timeframeBar QToolButton:checked { background: #303030; color: #ffffff; }
                QWidget#timeframeBar QToolButton:focus { border: 1px solid #666666; }
                QWidget#timeframeBar QToolButton:disabled { color: #6c6c6c; }
            """)

    def currentText(self):
        return self._current

    def setCurrentText(self, tf):
        if tf not in self.buttons:
            raise ValueError(f"Неизвестный ТФ: {tf}")
        self.buttons[tf].setChecked(True)
        if tf != self._current:
            self._current = tf
            self.currentTextChanged.emit(tf)


class MarketWorker(QThread):
    loaded = Signal(object, object, object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, symbol: str, tf: str, board: str, service: MarketService,
                 *, refresh=False, overrides=None, provenance=None):
        super().__init__()
        self.symbol, self.tf, self.board = symbol, tf, board
        self.service, self.refresh = service, refresh
        self.overrides, self.provenance = overrides, provenance

    def run(self):
        try:
            main, higher, info = self.service.view(
                self.symbol, self.board, self.tf, refresh=self.refresh,
                overrides=self.overrides, provenance=self.provenance,
                progress=self.progress.emit)
            self.loaded.emit(main, higher, info)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class CandleChart(QWidget):
    crosshair = Signal(int)

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(600, 430)
        self.analysis: Analysis | None = None
        self.parents: dict[str, Analysis] = {}
        self.times = []
        self.inside_bars: list[bool] = []
        self.show_structure = True
        self.show_fvg = True
        self.show_flow = True
        self.show_previous = True
        self.visual_theme = "tv"
        self.instrument_header = "DEMO · SYNTHETIC"
        self._zone_labels = []
        self.viewport = ChartView()
        self.price_range = None
        self.last_price_range = None
        self.include_flow_in_scale = False
        self.cursor_x: float | None = None
        self.cursor_y: float | None = None
        self.drag_origin = None
        self.drag_offset = 0
        self.drag_padding = 0
        self.drag_count = 0
        self.drag_mode = None
        self.drag_price = None
        self.x0 = 65
        self.x1 = 100
        self.top = 26
        self.bottom = 80

    def set_theme(self, theme: str):
        if theme not in CHART_PALETTES:
            raise ValueError(f"Неизвестная тема графика: {theme}")
        self.visual_theme = theme
        self.update()

    def set_analysis(self, analysis: Analysis, parents: dict[str, Analysis] | None = None):
        self.analysis = analysis
        self.parents = parents or {}
        self.times = [c.time.timestamp() for c in analysis.candles]
        self.inside_bars = inside_mother_bars(analysis.candles)
        self.offset = 0
        self.update()

    def window(self):
        n = len(self.analysis.candles) if self.analysis else 0
        return self.viewport.window(n)

    @property
    def offset(self):
        return self.viewport.offset

    @offset.setter
    def offset(self, value):
        self.viewport.offset = value

    def view_state(self):
        _, end = self.window()
        return {'count': self.viewport.count, 'offset': self.offset,
                'end': self.analysis.candles[end - 1].time.timestamp() if self.analysis and end and self.offset else None,
                'price_range': self.price_range, 'flow_scale': self.include_flow_in_scale,
                'right_padding': self.viewport.right_padding}

    def restore_view(self, state):
        self.viewport.count = state['count']
        self.offset = state['offset']
        if state['end'] is not None:
            ends = self.times
            end = bisect.bisect_right(ends, state['end'])
            self.offset = max(0, len(ends) - end) if end else 0
        self.price_range = state['price_range']
        self.include_flow_in_scale = state['flow_scale']
        self.viewport.right_padding = max(0, min(250, state.get('right_padding', 16)))
        self.update()

    def latest(self):
        self.viewport.latest()
        self.update()

    def auto_price(self):
        self.price_range = None
        self.include_flow_in_scale = False
        self.update()

    def fit_flow(self):
        self.price_range = None
        self.include_flow_in_scale = True
        self.update()

    def reset_view(self):
        self.viewport = ChartView()
        self.auto_price()

    def _zoom_price(self, factor, at_y):
        if self.last_price_range is None:
            return
        lo, hi = self.price_range or self.last_price_range
        fraction = min(1, max(0, (self.bottom - at_y) / max(1, self.bottom - self.top)))
        anchor = lo + fraction * (hi - lo)
        span = max(1e-8, (hi - lo) * factor)
        self.price_range = (anchor - fraction * span, anchor + (1 - fraction) * span)

    def wheelEvent(self, event):
        if not self.analysis:
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        if event.position().x() > self.x1 or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_price(0.82 if delta > 0 else 1.22, event.position().y())
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier or event.angleDelta().x():
            self.viewport.pan(round(self.viewport.count * (0.12 if delta > 0 else -0.12)), len(self.analysis.candles))
        else:
            # The right margin is a fixed number of empty slots; when
            # zooming, it cancels out of the pixel-to-candle anchor.
            self.viewport.zoom(0.82 if delta > 0 else 1.22,
                               (event.position().x() - self.x0) / max(1, self.x1 - self.x0),
                               len(self.analysis.candles))
        event.accept()
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        self.drag_origin = event.position()
        self.drag_offset = self.offset
        self.drag_padding = self.viewport.right_padding
        self.drag_count = self.viewport.count
        mx, my = event.position().x(), event.position().y()
        if mx > self.x1:
            self.drag_mode = "price_scale"
            self.drag_price = self.price_range or self.last_price_range
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif my > self.bottom and self.x0 <= mx <= self.x1:
            self.drag_mode = "time_scale"
            self.drag_price = None
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.drag_mode = "pan"
            self.drag_price = None
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_origin = None
        self.drag_price = None
        self.drag_mode = None
        self.unsetCursor()

    def mouseMoveEvent(self, event):
        self.cursor_x, self.cursor_y = event.position().x(), event.position().y()
        if self.drag_origin is not None and self.analysis:
            dx = self.cursor_x - self.drag_origin.x()
            dy = self.cursor_y - self.drag_origin.y()
            if self.drag_mode == "price_scale" and self.drag_price is not None:
                lo, hi = self.drag_price
                factor = math.exp(max(-5.0, min(5.0, dy * 0.009)))
                fraction = min(1.0, max(0.0, (self.bottom - self.drag_origin.y()) /
                                             max(1, self.bottom - self.top)))
                anchor = lo + fraction * (hi - lo)
                new_span = max(1e-8, (hi - lo) * factor)
                self.price_range = (anchor - fraction * new_span,
                                    anchor + (1.0 - fraction) * new_span)
            elif self.drag_mode == "time_scale":
                self.viewport.count = self.drag_count
                self.viewport.offset = self.drag_offset
                self.viewport.right_padding = self.drag_padding
                # Keep the same real candle under the cursor while the
                # visible candle count changes; right padding stays fixed.
                fraction = (self.drag_origin.x() - self.x0) / max(1, self.x1 - self.x0)
                factor = math.exp(max(-5.0, min(5.0, -dx * 0.009)))
                self.viewport.zoom(factor, fraction, len(self.analysis.candles))
            elif self.drag_mode == "pan":
                first, last = self.window()
                stride = (self.x1 - self.x0) / max(1, last - first + self.drag_padding)
                drag_bars = round(dx / max(0.01, stride))
                self.viewport.drag_chart(drag_bars, len(self.analysis.candles),
                                         start_offset=self.drag_offset,
                                         start_padding=self.drag_padding)
        if self.analysis:
            first, last = self.window()
            count = max(1, last - first)
            stride = (self.x1 - self.x0) / max(1, count + self.viewport.right_padding)
            index = first + math.floor((self.cursor_x - self.x0) / max(.01, stride))
            if first <= index < last:
                self.crosshair.emit(index)
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.position().x() > self.x1:
            self.auto_price()
        else:
            self.reset_view()

    def keyPressEvent(self, event):
        if not self.analysis:
            return
        key = event.key()
        if key == Qt.Key.Key_End:
            self.latest()
        elif key == Qt.Key.Key_Home:
            self.offset = max(0, len(self.analysis.candles) - self.viewport.count)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.viewport.pan(round(self.viewport.count * (0.15 if key == Qt.Key.Key_Left else -0.15)), len(self.analysis.candles))
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self.viewport.zoom(1.22 if key == Qt.Key.Key_Minus else 0.82, 0.5, len(self.analysis.candles))
        elif key in (Qt.Key.Key_0, Qt.Key.Key_F):
            self.reset_view() if key == Qt.Key.Key_0 else self.auto_price()
        else:
            return super().keyPressEvent(event)
        self.update()

    def leaveEvent(self, event):
        self.cursor_x = self.cursor_y = None
        self.update()

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = CHART_PALETTES[self.visual_theme]
        p.fillRect(self.rect(), colors["background"])
        if self.analysis is None or not self.analysis.candles:
            p.setPen(colors["text"])
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
        stride = span / max(1, count + self.viewport.right_padding)
        def x(index: int):
            return self.x0 + (index - first + 0.5) * stride
        visible = candles[first:last]
        lowest = min(b.low for b in visible)
        highest = max(b.high for b in visible)
        if self.include_flow_in_scale and self.analysis.flows:
            st = self.analysis.flows[last - 1]
            if st.lo is not None:
                lowest, highest = min(lowest, st.lo), max(highest, st.hi)
        gap = max(0.001, (highest - lowest) * 0.075)
        lowest -= gap
        highest += gap
        if self.price_range is not None:
            lowest, highest = self.price_range
        self.last_price_range = (lowest, highest)
        def y(value: float):
            return self.bottom - (value - lowest) / max(1e-8, highest - lowest) * (self.bottom - self.top)
        p.setFont(QFont("Segoe UI", 9))
        for j in range(7):
            value = lowest + (highest - lowest) * j / 6
            Y = y(value)
            p.setPen(QPen(colors["grid"], 0.8))
            p.drawLine(QPointF(self.x0, Y), QPointF(self.x1, Y))
            p.setPen(colors["text"])
            p.drawText(QRectF(self.x1 + 8, Y - 11, 80, 20), 0, short_number(value))
        for j in range(7):
            idx = first + int(j * (count - 1) / 6)
            p.setPen(QPen(colors["grid"], 0.8))
            p.drawLine(QPointF(x(idx), self.top), QPointF(x(idx), self.bottom))
            p.setPen(colors["text"])
            p.drawText(QRectF(x(idx) - 52, self.bottom + 10, 104, 20), int(Qt.AlignmentFlag.AlignCenter), candles[idx].time.strftime("%d.%m %H:%M" if self.analysis.timeframe in ("M1", "M5", "H1", "H4") else "%d.%m.%Y"))
        p.save()
        p.setClipRect(QRectF(self.x0, self.top, self.x1 - self.x0, self.bottom - self.top))
        # FVG backgrounds and edges: local zones preserve break candle bounds.
        # Labels are de-duplicated by position for adjacent/overlapping zones.
        self._zone_labels = []
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
                times = self.times
                for z in parent.published_fvg[-14:]:
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
                pen = QPen(colors["structure_up"] if seg.side == 1
                           else colors["structure_down"], 1.6 if self.visual_theme == "tv" else 2.2)
                if not seg.confirmed:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(x(a), y(ya)), QPointF(x(b), y(yb)))
        # TV mode uses white/black bodies with dark outlines and thin wicks.
        # Candles are painted after the transparent structure and FVG layers.
        for i in range(first, last):
            bar = candles[i]
            rising = bar.close >= bar.open
            color = colors["candle_up"] if rising else colors["candle_down"]
            center = x(i)
            # ONLY inside candles are hollow, regardless of O/C direction.
            # A non-inside mother candle stays solid: white if rising, black
            # if falling. Internal status uses the last non-inside mother,
            # not just the immediately preceding candle.
            hollow = (self.visual_theme == "tv" and
                      i < len(self.inside_bars) and self.inside_bars[i])
            wick_color = (QColor("#ededed") if rising else QColor("#242424"))
            p.setPen(QPen(wick_color if self.visual_theme == "tv"
                          else (colors["wick"] or color),
                          0.9 if self.visual_theme == "tv" else 1.2))
            p.drawLine(QPointF(center, y(bar.high)), QPointF(center, y(bar.low)))
            body_w = max(2.0, min(18.0, stride * 0.62))
            a, b = y(bar.open), y(bar.close)
            body = QRectF(center - body_w / 2, min(a, b),
                          body_w, max(1.3, abs(b - a)))
            if not hollow:
                p.fillRect(body, color)
            if self.visual_theme == "tv":
                outline = (QColor("#e4e4e4") if rising else QColor("#474747")) if hollow else (
                    colors["candle_up_outline"] if rising else
                    colors["candle_down_outline"])
                p.setPen(QPen(outline, 1.0))
                p.drawRect(body)
        # Current and previous flow 0, 0.4, 0.5, 0.6, 1.0.
        if self.show_flow and self.analysis.flows:
            st = self.analysis.flows[last - 1]
            if st.side and st.lo is not None and st.hi is not None:
                starts = max(first, st.start or first)
                for t, color in zip((0.0, 0.4, 0.5, 0.6, 1.0),
                                    colors["flow_lines"]):
                    price = st.lo + (st.hi - st.lo) * t
                    p.setPen(QPen(color, 1.25 if self.visual_theme == "tv" else 1.5))
                    Y = y(price)
                    p.drawLine(QPointF(x(starts), Y), QPointF(self.x1, Y))
                    p.setFont(QFont("Segoe UI", 8))
                    caption = (f"{t:g} ({short_number(price)})"
                               if self.visual_theme == "tv" else str(t))
                    p.drawText(QRectF(self.x1 - 122, Y - 17, 116, 15),
                               int(Qt.AlignmentFlag.AlignRight), caption)
            if self.show_previous and st.previous:
                old_start, old_end, old_lo, old_hi, _ = st.previous
                if old_lo is not None and old_hi is not None:
                    for r in (0.0, 0.4, 0.5, 0.6, 1.0):
                        Y = y(old_lo + (old_hi - old_lo) * r)
                        pen = QPen(colors["previous"], 0.9)
                        pen.setStyle(Qt.PenStyle.DashLine)
                        p.setPen(pen)
                        p.drawLine(QPointF(x(max(first, old_start)), Y), QPointF(x(min(last - 1, max(first, old_end))), Y))
        p.restore()
        # Prevent price-axis tags from overlapping when the crosshair is
        # hovering near the latest close (only presentation, no data mutation).
        latest = candles[last - 1]
        color = colors["candle_up"] if latest.close >= latest.open else colors["candle_down"]
        last_price_y = min(self.bottom, max(self.top, y(latest.close)))
        hover_on_chart = (self.cursor_x is not None and self.cursor_y is not None
                          and self.x0 <= self.cursor_x <= self.x1
                          and self.top <= self.cursor_y <= self.bottom)
        if not (hover_on_chart and abs(self.cursor_y - last_price_y) < 22):
            p.fillRect(QRectF(self.x1 + 3, last_price_y - 10, 82, 20),
                       colors["price_marker"] or color)
            p.setPen(colors["price_marker_text"])
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(QRectF(self.x1 + 5, last_price_y - 10, 80, 20),
                       int(Qt.AlignmentFlag.AlignCenter), short_number(latest.close))
        p.setPen(colors["text"])
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        hover_i = first + math.floor((self.cursor_x - self.x0) / max(.01, stride)) if self.cursor_x is not None else -1
        header_bar = candles[hover_i] if first <= hover_i < last and self.cursor_y is not None and self.top <= self.cursor_y <= self.bottom else latest
        p.drawText(QRectF(self.x0, 0, max(150, self.width() - 120), 23), 0,
                   f"{self.instrument_header}   ·   {self.analysis.timeframe}   {header_bar.time:%d.%m %H:%M}   O {short_number(header_bar.open)}   H {short_number(header_bar.high)}   L {short_number(header_bar.low)}   C {short_number(header_bar.close)}")
        if self.cursor_x is not None and self.x0 <= self.cursor_x <= self.x1 and self.top <= self.cursor_y <= self.bottom:
            pen = QPen(colors["crosshair"], 0.85, Qt.PenStyle.DashLine)
            p.setPen(pen)
            snap_x = x(min(last - 1, max(first, hover_i)))
            p.drawLine(QPointF(snap_x, self.top), QPointF(snap_x, self.bottom))
            p.drawLine(QPointF(self.x0, self.cursor_y), QPointF(self.x1, self.cursor_y))
            price = lowest + (self.bottom - self.cursor_y) / max(1, self.bottom - self.top) * (highest - lowest)
            p.fillRect(QRectF(self.x1 + 3, self.cursor_y - 10, 82, 20),
                       colors["tooltip_bg"])
            p.setPen(colors["tooltip_text"])
            p.drawText(QRectF(self.x1 + 5, self.cursor_y - 10, 80, 20),
                       int(Qt.AlignmentFlag.AlignCenter), short_number(price))
            p.fillRect(QRectF(snap_x - 65, self.bottom + 5, 130, 22),
                       colors["tooltip_bg"])
            p.drawText(QRectF(snap_x - 65, self.bottom + 5, 130, 22),
                       int(Qt.AlignmentFlag.AlignCenter),
                       header_bar.time.strftime('%d.%m.%Y %H:%M'))
        p.setPen(colors["text"])
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(self.x0, self.height() - 32, self.width() - 170, 20), 0,
                   "Перетащи: график / время / цену · Влево за последней свечой: пустое пространство · End: последние")
        p.end()

    def _draw_zone(self, p, x0, x1, y0, y1, side, broken, tf, stride, parent=False):
        """Draw transparent FVG with a right-aligned, collision-aware caption."""
        if x1 <= x0:
            return
        low_y, high_y = min(y0, y1), max(y0, y1)
        height = max(1.5, high_y - low_y)
        if self.visual_theme == "tv":
            if broken:
                bg, edge = QColor(176, 135, 68, 16), QColor(117, 94, 58, 75)
            elif side == 1:
                bg, edge = QColor(26, 111, 89, 34 if parent else 26), QColor(16, 89, 72, 115)
            else:
                bg, edge = QColor(151, 42, 57, 45 if parent else 35), QColor(131, 38, 50, 115)
        else:
            if broken:
                bg, edge = QColor(243, 169, 45, 40), QColor(241, 173, 70, 130)
            elif side == 1:
                bg, edge = QColor(24, 196, 145, 38 if not parent else 28), QColor(19, 148, 123, 135)
            else:
                bg, edge = QColor(221, 70, 100, 38 if not parent else 28), QColor(204, 74, 95, 135)
        p.fillRect(QRectF(x0, low_y, x1 - x0, height), bg)
        p.setPen(QPen(edge, 0.85))
        p.drawRect(QRectF(x0, low_y, x1 - x0, height))
        p.setPen(QPen(edge, 0.75, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(x0, (y0 + y1) / 2), QPointF(x1, (y0 + y1) / 2))
        if x1 - x0 >= 68 and height >= 18:
            short_tf = {"D1": "D", "W1": "W", "MN1": "M"}.get(tf, tf)
            label = f"FVG {short_tf}"
            width = min(98.0, x1 - x0 - 9.0)
            label_rect = QRectF(x1 - width - 4, low_y + (height - 16) / 2, width, 16)
            if not any(label_rect.intersects(existing) for existing in self._zone_labels):
                self._zone_labels.append(label_rect)
                p.setPen(edge if self.visual_theme == "tv" else CHART_PALETTES["dark"]["text"])
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(label_rect, int(Qt.AlignmentFlag.AlignRight |
                                           Qt.AlignmentFlag.AlignVCenter), label)


class MainWindow(QMainWindow):
    def __init__(self, service=None):
        super().__init__()
        self.setWindowTitle(f"FlowDesk {__version__} — Структура и FVG · Windows")
        self.setMinimumSize(1060, 690)
        self.resize(1420, 830)
        self.worker: MarketWorker | None = None
        self.market_service = service or MarketService()
        self.pending_market = None
        self.desired_market = None
        self.loaded_market = None
        self.data_info = {}
        self.chart_views = {}
        self.csv_overrides, self.csv_provenance = {}, {}
        self.local_series, self.local_provenance = {}, {}
        self.closing_after_load = False
        self.source = "DEMO (синтетические свечи)"
        self.analysis: Analysis | None = None
        self.parents: dict[str, Analysis] = {}
        self.chart = CandleChart()
        self._build_ui()
        self.auto_timer = QTimer(self)
        self.auto_timer.setSingleShot(True)
        self.auto_timer.setInterval(60000)
        self.auto_timer.timeout.connect(self._auto_refresh)
        self.auto_checkbox.toggled.connect(self._schedule_auto_refresh)
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
        self.symbol.returnPressed.connect(self.load_moex)
        self.tf = TimeframeBar()
        header.addWidget(self.tf)
        self.load_button = QPushButton("Обновить MOEX")
        self.load_button.clicked.connect(self.load_moex)
        header.addWidget(self.load_button)
        self.csv_button = QPushButton("Открыть CSV")
        self.csv_button.clicked.connect(self.open_csv)
        header.addWidget(self.csv_button)
        self.h1_button = QPushButton("Добавить старший CSV")
        self.h1_button.setToolTip("H1 для M1/M5; H4/D1/W1/MN1 для старших зон. CSV того же инструмента и сессии.")
        self.h1_button.clicked.connect(self.open_parent_csv)
        header.addWidget(self.h1_button)
        self.export_button = QPushButton("Экспорт диагностики")
        self.export_button.clicked.connect(self.export_diagnostics)
        header.addWidget(self.export_button)
        header.addStretch()
        layout.addLayout(header)
        navigation = QHBoxLayout()
        for caption, callback in [('К последним · End', self.chart.latest),
                                  ('Автоцена · F', self.chart.auto_price),
                                  ('Диапазон потока', self.chart.fit_flow),
                                  ('Сброс вида · 0', self.chart.reset_view)]:
            button = QPushButton(caption)
            button.clicked.connect(callback)
            navigation.addWidget(button)
        self.auto_checkbox = QCheckBox("Авто · 60 с")
        self.auto_checkbox.setChecked(True)
        self.auto_checkbox.setToolTip("Обновлять открытый инструмент MOEX через минуту после завершения запроса. Данные ISS поступают с задержкой около 15 минут.")
        navigation.addWidget(self.auto_checkbox)
        navigation.addSpacing(10)
        self.panel_checkbox = QCheckBox("Панель")
        self.panel_checkbox.setToolTip("Показать диагностику, последние FVG и события")
        navigation.addWidget(self.panel_checkbox)
        navigation.addStretch()
        self.data_age = QLabel('')
        navigation.addWidget(self.data_age)
        layout.addLayout(navigation)
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
        self.right_panel = right
        self.panel_checkbox.toggled.connect(self.right_panel.setVisible)
        # TV preset gives the price chart the maximum available width.
        self.right_panel.hide()
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.chart.crosshair.connect(self._hover)
        self.tf.currentTextChanged.connect(self._timeframe_changed)
        self._style()

    def _style(self):
        """Permanent dark TradingView-like chrome around a neutral-grey chart."""
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #1c1c1c; color: #d1d1d1;
                font-family: 'Segoe UI'; font-size: 12px;
            }
            #brand {
                font-size: 18px; font-weight: 800; color: #f2f2f2;
                letter-spacing: 1px;
            }
            #section { font-weight: 700; font-size: 13px; color: #b5b5b5; margin-top: 6px; }
            #warning {
                background: #2b2623; color: #d6b89c;
                border: 1px solid #474038; padding: 6px; border-radius: 3px;
            }
            #bigPrice { font-size: 24px; font-weight: 700; color: #f2f2f2; }
            #status { color: #979797; font-size: 11px; }
            QLineEdit, QComboBox, QPushButton {
                background: #292929; color: #e5e5e5;
                border: 1px solid #424242; padding: 7px; border-radius: 4px;
            }
            QPushButton:hover { background: #3a3a3a; border: 1px solid #656565; }
            QPushButton:pressed { background: #505050; }
            QPushButton:disabled { color: #777777; background: #242424; }
            QPlainTextEdit, QTableWidget {
                background: #202020; color: #dddddd;
                border: 1px solid #373737; gridline-color: #393939;
            }
            QHeaderView::section {
                background: #303030; color: #c9c9c9; padding: 5px; border: 0px;
            }
            QCheckBox { spacing: 9px; padding: 3px; }
            QSplitter::handle { background: #313131; width: 3px; }
        """)
        self.tf.set_theme("dark")

    def _toggle(self, key: str, value: bool):
        setattr(self.chart, key, value)
        self.chart.update()

    def load_moex(self):
        self._request_market(refresh=True)

    def _schedule_auto_refresh(self, *_):
        self.auto_timer.stop()
        if (self.auto_checkbox.isChecked() and self.loaded_market and
                self.source.startswith('MOEX:') and self.worker is None and not self.closing_after_load):
            self.auto_timer.start()

    def _auto_refresh(self):
        if (not self.auto_checkbox.isChecked() or not self.loaded_market or
                not self.source.startswith('MOEX:') or self.worker is not None or self.closing_after_load):
            return
        # Never open an unsubmitted ticker/board or replace a queued user request.
        context = (self.symbol.text().strip().upper(), self.board.currentText(), self.tf.currentText())
        if context != self.loaded_market:
            self._schedule_auto_refresh()
            return
        self.desired_market = self.loaded_market
        self._start_market((*self.loaded_market, True), automatic=True)

    def _timeframe_changed(self, tf):
        if self.source.startswith('MOEX:') or self.worker is not None:
            self._request_market(refresh=False)
        elif self.analysis and tf != self.analysis.timeframe:
            series = self.local_series or {self.analysis.timeframe: self.analysis.candles,
                                           **{t: a.candles for t, a in self.parents.items()}}
            if tf in series:
                provenance = self.local_provenance or {self.analysis.timeframe: self.analysis.session_info,
                                                       **{t: a.session_info for t, a in self.parents.items()}}
                a, parents = analyze_timeframes(series, tf, asof=self.analysis.asof, provenance=provenance)
                self._show_data(a, parents, self.source)
            else:
                self.tf.blockSignals(True)
                self.tf.setCurrentText(self.analysis.timeframe)
                self.tf.blockSignals(False)
                self.status.setText(f'Для {tf} загрузите инструмент MOEX или соответствующий CSV')

    def _request_market(self, *, refresh):
        symbol = self.symbol.text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Тикер", "Введите тикер, например SBER")
            return
        request = (symbol, self.board.currentText(), self.tf.currentText(), refresh)
        self.desired_market = request[:3]
        if self.worker is not None:
            self.pending_market = request
            self.status.setText(f'Следующий график: {symbol} · {request[2]}; завершение текущей загрузки')
            return
        self._start_market(request)

    def _start_market(self, request, *, automatic=False):
        self.auto_timer.stop()
        symbol, board, tf, refresh = request
        if self.loaded_market is None or self.loaded_market[:2] != request[:2]:
            self.csv_overrides, self.csv_provenance = {}, {}
        self.load_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        self.h1_button.setEnabled(False)
        self.status.setText(f'Открытие {symbol} · {tf}…')
        self.worker = MarketWorker(symbol, tf, board, self.market_service, refresh=refresh,
                                   overrides=dict(self.csv_overrides), provenance=dict(self.csv_provenance))
        self.worker.automatic = automatic
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(self._failed)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self._market_finished)
        self.worker.start()

    def _loaded(self, analysis: Analysis, higher: dict, info: dict):
        context = (info['symbol'], info['board'], info['tf'])
        if context != self.desired_market or self.closing_after_load:
            return
        self.loaded_market = context
        self.data_info = info
        self._show_data(analysis, higher, f"MOEX: {info['symbol']} / {info['board']}")

    def _failed(self, error: str):
        self.status.setText(f"Ошибка загрузки: {error}")
        if not self.pending_market and not self.closing_after_load and not getattr(self.worker, 'automatic', False):
            QMessageBox.warning(self, "MOEX ISS", error)

    def _market_finished(self):
        finished = self.sender()
        if finished is not self.worker:
            finished.deleteLater()
            return
        automatic = getattr(finished, 'automatic', False)
        self.worker = None
        if finished:
            finished.deleteLater()
        if self.closing_after_load:
            self.close()
            return
        if self.pending_market:
            request, self.pending_market = self.pending_market, None
            self._start_market(request)
            return
        for control in (self.load_button, self.csv_button, self.h1_button, self.export_button):
            control.setEnabled(True)
        if self.analysis:
            self.tf.blockSignals(True)
            self.tf.setCurrentText(self.analysis.timeframe)
            self.tf.blockSignals(False)
            if self.loaded_market and not automatic:
                self.symbol.setText(self.loaded_market[0])
                self.board.setCurrentText(self.loaded_market[1])
        self._schedule_auto_refresh()

    def closeEvent(self, event):
        self.auto_timer.stop()
        if self.worker is not None:
            self.closing_after_load = True
            self.pending_market = None
            self.hide()
            event.ignore()
        else:
            event.accept()

    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать экспорт TradingView", "", "CSV (*.csv);;Все файлы (*.*)")
        if not path:
            return
        try:
            candles = read_csv(path)
            detected = infer_timeframe(candles)
            tf = detected or self.tf.currentText()
            if tf in ("M1", "M5", "H1"):
                candles = exclude_opening_auction(candles, tf,
                                                  board=self.board.currentText(),
                                                  overrides=load_session_overrides())
            if not candles:
                raise ValueError("В CSV не осталось свечей после фильтрации аукциона")
            self.data_info, self.csv_overrides, self.csv_provenance = {}, {}, {}
            self.loaded_market = None
            self.local_series = {tf: candles}
            self.local_provenance = {tf: {'mode': 'USER_CSV', 'file': Path(path).name}}
            # Never apply a prior H1 CSV from another instrument to a newly
            # opened lower-TF CSV. User explicitly imports the matching H1.
            analysis, _ = analyze_timeframes({tf: candles}, tf)
            self._show_data(analysis, {}, f"CSV: {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка CSV", str(exc))

    def open_parent_csv(self):
        if not self.analysis:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Старшие CSV того же инструмента и торговой сессии", "", "CSV (*.csv);;Все файлы (*.*)")
        if not paths:
            return
        try:
            main = self.analysis
            series = {tf: parent.candles for tf, parent in self.parents.items()}
            series[main.timeframe] = main.candles
            provenance = {tf: parent.session_info for tf, parent in self.parents.items()}
            provenance[main.timeframe] = main.session_info
            allowed = [tf for tf in ("H1", "H4", "D1", "W1", "MN1")
                       if TIMEFRAMES.index(tf) > TIMEFRAMES.index(main.timeframe)]
            if not allowed:
                raise ValueError("Для MN1 старший таймфрейм не предусмотрен")
            imported = set()
            for path in paths:
                candles = read_csv(path)
                tf = infer_timeframe(candles)
                if tf is None:
                    tf, accepted = QInputDialog.getItem(
                        self, "Таймфрейм CSV", f"ТФ файла {Path(path).name}:", allowed, 0, False)
                    if not accepted:
                        return
                if tf not in allowed or tf in imported:
                    raise ValueError(f"Недопустимый или повторный старший ТФ: {tf}")
                if tf == "H1":
                    candles = exclude_opening_auction(
                        candles, tf, board=self.board.currentText(), overrides=load_session_overrides())
                if not candles:
                    raise ValueError("В CSV не осталось свечей")
                # A long-term period may open before the visible main range.
                if (candle_close_time(candles[-1], tf) <= main.candles[0].time or
                        candles[0].time > main.candles[-1].time):
                    raise ValueError(f"{Path(path).name}: периоды не пересекаются. Проверьте тикер и даты.")
                series[tf] = candles
                provenance[tf] = {"mode": "USER_CSV", "file": Path(path).name,
                                  "session": "USER_SELECTED", "symbol_verified": False,
                                  "observed_asof": (main.asof or datetime.now(timezone.utc)).isoformat()}
                imported.add(tf)
            updated, parents = analyze_timeframes(series, main.timeframe, asof=main.asof, provenance=provenance)
            if self.source.startswith('MOEX:'):
                self.csv_overrides.update({tf: series[tf] for tf in imported})
                self.csv_provenance.update({tf: provenance[tf] for tf in imported})
            else:
                self.local_series.update(series)
                self.local_provenance.update(provenance)
            updated.flow_source_note += "; добавлены старшие CSV того же инструмента (тикер проверяется вручную)"
            self._show_data(updated, parents, self.source)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка импорта старших CSV", str(exc))

    def _show_data(self, analysis: Analysis, parents: dict, source: str):
        if self.analysis:
            self.chart_views[(self.source, self.analysis.timeframe)] = self.chart.view_state()
        self.source = source
        self.auto_checkbox.setEnabled(source.startswith('MOEX:'))
        if not source.startswith('MOEX:'):
            self.auto_timer.stop()
        self.analysis = analysis
        self.parents = parents
        if source.startswith("MOEX:"):
            ticker = source.split(":", 1)[1].split("/", 1)[0].strip()
            self.chart.instrument_header = f"{ticker} · MOEX"
        elif source.startswith("CSV:"):
            self.chart.instrument_header = "CSV · ИМПОРТ"
        else:
            self.chart.instrument_header = "DEMO · СИНТЕТИКА"
        self.chart.set_analysis(analysis, parents)
        saved_view = self.chart_views.get((source, analysis.timeframe))
        if saved_view:
            self.chart.restore_view(saved_view)
        else:
            self.chart.reset_view()
        self.tf.blockSignals(True)
        self.tf.setCurrentText(analysis.timeframe)
        self.tf.blockSignals(False)
        state = analysis.flows[-1]
        price = analysis.candles[-1].close
        flow_origin = ("H1, только закрытые свечи" if analysis.timeframe in ("M1", "M5") else
                       f"{analysis.timeframe}, только закрытые свечи")
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
        def at(dt):
            return dt.strftime("%d.%m %H:%M") if dt is not None else "—"
        origin_line = (
            f"<br>0 от {analysis.flow_source_tf}: {at(state.range_low_at)}"
            f" · 1 от {analysis.flow_source_tf}: {at(state.range_high_at)}"
            if state.side != 0 else ""
        )
        self.state_label.setText(f"Диапазон: {short_number(state.lo) if state.lo is not None else '—'} — {short_number(state.hi) if state.hi is not None else '—'}"
                                 f"{origin_line}<br>Рабочий FVG: {'есть' if state.guard and not state.guard_broken else 'нет'}"
                                 f"<br>Пара IDM: {self._pair_text(state)}"
                                 f"<br>№1: включено · приоритет №2 → №1 → №3")
        events = [(analysis.candles[s.i].time.strftime("%d.%m %H:%M"), s.diagnosis) for s in analysis.flows if s.rule in (1, 2, 3)]
        self.events.setPlainText("\n".join(f"{t}  {reason}" for t, reason in events[-18:][::-1]) or "Нет подтверждённых событий №1/№2/№3 (или ряд H1 отсутствует)")
        zones = analysis.fvg[-17:][::-1]
        self.zones_table.setRowCount(len(zones))
        for i, z in enumerate(zones):
            values = [f"{z.source_tf} {'↑' if z.side == 1 else '↓'} {'×' if not z.active else ''}", short_number(z.bottom), short_number(z.top)]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setForeground(UP if z.side == 1 else DOWN)
                self.zones_table.setItem(i, col, item)
        session_desc = (" · Готовые свечи MOEX, включая аукцион" if analysis.session_info.get("native_higher")
                        else " · Аукцион открытия исключён" if analysis.session_info.get("opening_auction_excluded") else "")
        coverage = " · ".join(
            f"{tf}: {p.confirmed_bars} закрытых ({p.candles[0].time:%d.%m.%Y}–{p.candles[-1].time:%d.%m.%Y})"
            for tf, p in parents.items() if p.candles and tf in ("D1", "W1", "MN1"))
        self.status.setText(f"{source} · {len(analysis.candles)} свечей ({analysis.confirmed_bars} закрытых) · {len(analysis.fvg)} FVG{session_desc} · {analysis.flow_source_note} · {coverage} · НЕ сверено с Pine")
        if self.data_info:
            observed = datetime.fromisoformat(self.data_info['asof']).astimezone(MOSCOW)
            origin = {'memory': 'Сохранённые данные', 'disk': 'Сохранённые данные',
                      'offline': 'Офлайн', 'network': 'Обновлено'}[self.data_info['tier']]
            requested = datetime.fromisoformat(self.data_info.get('requested_at', self.data_info['asof'])).astimezone(MOSCOW)
            delay = self.data_info.get('feed_delay_seconds', 0)
            self.data_age.setText(f'{origin} · {requested:%H:%M:%S} МСК' + (f' · задержка ≈{delay // 60} мин' if delay else ''))
            self.data_age.setToolTip(f'Начало запроса: {requested:%d.%m.%Y %H:%M:%S} МСК\nРасчёт закрытых свечей до: {observed:%d.%m.%Y %H:%M:%S} МСК')
            if self.data_info.get('warning'):
                self.status.setText(self.status.text() + ' · ' + self.data_info['warning'])
        else:
            self.data_age.setText('')

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
                "data_snapshot": self.data_info,
                "calculation": source_coverage(a),
                "session_info": a.session_info if a.session_info else {
                    "mode": "CSV_SESSION_FILTERED_FOR_M1_M5_H1_OR_ASSUMED_FOR_HIGHER",
                    "warning": "Для H4 CSV проверьте сессию; D/W/M сохраняют предоставленные OHLCV, включая аукцион"},
                "caveats": ["Rules 2 > 1 > 3 applied; full Pine parity unverified", "Quotes may be delayed", "Special MOEX sessions/older TF history need TradingView session parity checks"],
                "bars": len(a.candles),
                "candles": [serialize_candle(c) for c in a.candles],
                "flows": [{"time": c.time.isoformat(), "side": f.side, "lo": f.lo, "hi": f.hi, "start": f.start, "rule": f.rule, "diagnosis": f.diagnosis,
                           "bar_confirmed": a.is_confirmed(i), "live_range": f.live_range,
                           "source_tf": f.source_tf or a.flow_source_tf,
                           "source_bar_open": f.source_bar_time.isoformat() if f.source_bar_time else None,
                           "source_closed_at": f.source_confirmed_at.isoformat() if f.source_confirmed_at else None,
                           "source_h1_open": f.source_bar_time.isoformat() if f.source_bar_time and f.source_tf == "H1" else None,
                           "source_h1_closed_at": f.source_confirmed_at.isoformat() if f.source_confirmed_at and f.source_tf == "H1" else None,
                           "range_start_at": f.range_start_at.isoformat() if f.range_start_at else None,
                           "range_low_at": f.range_low_at.isoformat() if f.range_low_at else None,
                           "range_high_at": f.range_high_at.isoformat() if f.range_high_at else None,
                           "last_change_at": f.last_change_at.isoformat() if f.last_change_at else None,
                           "last_change_rule": f.last_change_rule,
                           "range_origin": f.range_origin,
                           "rule1_check": f.rule1_check,
                           "change_evidence": f.change_evidence}
                          for i, (c, f) in enumerate(zip(a.candles, a.flows))],
                "fvg": [{"time": a.candles[z.created].time.isoformat(), "start": z.start, "side": z.side, "top": z.top, "bottom": z.bottom, "balanced": z.balanced, "broken_at": z.broken} for z in a.fvg],
                "rule1_higher_liquidity": serialize_audit(a.liquidity_audit if a.timeframe not in ("M1", "M5") else
                                                         self.parents["H1"].liquidity_audit if "H1" in self.parents else []),
                "rule1_mode": "APPLIED_TO_CONFIRMED_FLOW",
                "higher_timeframes": {
                    tf: {
                        "source": ("MOEX_NATIVE_CANDLES" if p.session_info.get("native_higher") else "MOEX_CLEAN_H1_PHYSICAL_CALENDAR"
                                   if p.session_info.get("higher_from_clean_h1") else "CSV_OR_SUPPLIED_HIGHER_TF"),
                        "source_info": p.session_info,
                        "coverage": source_coverage(p),
                        "rule1_higher_liquidity": serialize_audit(p.liquidity_audit),
                        "bars": len(p.candles),
                        "candles": [serialize_candle(c) for c in p.candles],
                        "pairs": [{"time": c.time.isoformat(),
                                   "high": pair.high, "low": pair.low,
                                   "high_key": pair.high_key, "low_key": pair.low_key,
                                   "direction": pair.direction}
                                  for c, pair in zip(p.candles, p.pairs)],
                        "fvg": [{"formed_at": p.candles[z.created].time.isoformat(),
                                 "side": z.side, "bottom": z.bottom,
                                 "top": z.top, "balanced": z.balanced,
                                 "broken_at": (p.candles[z.broken].time.isoformat()
                                               if z.broken is not None and
                                               0 <= z.broken < len(p.candles) else None)}
                                for z in p.fvg if 0 <= z.created < len(p.candles)],
                        "flows": [{"time": c.time.isoformat(),
                                   "side": f.side, "lo": f.lo, "hi": f.hi,
                                   "rule": f.rule,
                                   "bar_confirmed": p.is_confirmed(i),
                                   "live_range": f.live_range,
                                   "source_closed_at": (f.source_confirmed_at.isoformat()
                                                        if f.source_confirmed_at else None),
                                   "range_start_at": (f.range_start_at.isoformat()
                                                      if f.range_start_at else None),
                                   "rule1_check": f.rule1_check,
                                   "change_evidence": f.change_evidence,
                                   "last_change_at": (f.last_change_at.isoformat()
                                                      if f.last_change_at else None)}
                                  for i, (c, f) in enumerate(zip(p.candles, p.flows))]
                    }
                    for tf in ("H4", "D1", "W1", "MN1")
                    for p in [a if a.timeframe == tf else self.parents.get(tf)]
                    if p is not None
                },
                "h1_reference": (None if "H1" not in self.parents else {
                    "source": ("User CSV H1" if self.parents["H1"].session_info.get("mode") == "USER_CSV"
                               else "MOEX separate H1" if self.source.startswith("MOEX:") else "User CSV H1"),
                    "source_info": self.parents["H1"].session_info,
                    "coverage": source_coverage(self.parents["H1"]),
                    "bars": len(self.parents["H1"].candles),
                    "events": [{"time": self.parents["H1"].candles[f.i].time.isoformat(), "side": f.side,
                                "lo": f.lo, "hi": f.hi, "rule": f.rule,
                                "range_origin": f.range_origin,
                                "range_start_at": f.range_start_at.isoformat() if f.range_start_at else None,
                                "range_low_at": f.range_low_at.isoformat() if f.range_low_at else None,
                                "range_high_at": f.range_high_at.isoformat() if f.range_high_at else None}
                               for f in self.parents["H1"].flows if f.rule in (1, 2, 3)],
                    "candles": [{"time": c.time.isoformat(), "open": c.open, "high": c.high,
                                 "low": c.low, "close": c.close, "volume": c.volume}
                                for c in self.parents["H1"].candles],
                    "flows": [{"time": c.time.isoformat(), "side": f.side, "lo": f.lo, "hi": f.hi,
                               "rule": f.rule, "start": f.start,
                               "range_start_at": f.range_start_at.isoformat() if f.range_start_at else None,
                               "range_low_at": f.range_low_at.isoformat() if f.range_low_at else None,
                               "range_high_at": f.range_high_at.isoformat() if f.range_high_at else None,
                               "last_change_at": f.last_change_at.isoformat() if f.last_change_at else None,
                               "last_change_rule": f.last_change_rule,
                               "range_origin": f.range_origin}
                              for c, f in zip(self.parents["H1"].candles, self.parents["H1"].flows)]
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
