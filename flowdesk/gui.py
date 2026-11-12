"""Qt Widgets desktop UI with an interactive candlestick chart.

The code contains no order/trade APIs. All calculations are explicitly marked
as experimental until the user-provided TradingView CSV tests establish parity.
"""
from __future__ import annotations

import bisect
import json
import math
import random
from dataclasses import replace
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
from .chart_view import ChartView, inside_mother_bars, time_axis_zoom_factor
from .chart_time import candle_time_text
from .fvg_display import DisplaySettings, select_fvg_display
from .runtime_errors import record_exception, last_exception
from .market_cache import MarketService
from .stream_worker import StreamWorker
from .stream_ui import StreamController
from .watchlist_ui import WatchlistController
from .watchlist import WatchlistService
from .preferences import Preferences
from .settings import IndicatorSettings,ApplicationSettings,SettingsStore,MemorySettingsStore,SIZES
from .settings_dialog import SettingsDialog,qcolor
from .chart_indicators import vwap_series
from .price_format import price_text
from .indicator_paint import draw_market,draw_vwap,draw_overlays,font as indicator_font
from .cancellation import Cancelled, checkpoint, scope
from .lifecycle import ShutdownWatchdog
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
                 *, refresh=False, overrides=None, provenance=None, stream_batch=(), repair=False, settings=None):
        super().__init__()
        self.symbol, self.tf, self.board = symbol, tf, board
        self.service, self.refresh = service, refresh
        self.overrides, self.provenance = overrides, provenance
        self.stream_batch, self.repair = stream_batch, repair
        self.settings=settings

    def run(self):
        try:
            with scope(self.isInterruptionRequested):
                if self.stream_batch:
                    self.service.ingest_stream(self.symbol, self.board, self.stream_batch)
                extra = {'with_minutes': True} if self.repair else {}
                if self.settings is not None and isinstance(self.service,MarketService):
                    extra['settings']=self.settings
                def progress(message):
                    checkpoint()
                    self.progress.emit(message)
                main, higher, info = self.service.view(
                    self.symbol, self.board, self.tf, refresh=self.refresh,
                    overrides=self.overrides, provenance=self.provenance,
                    progress=progress, **extra)
                checkpoint()
                if self.stream_batch:
                    info['tier'] = 'stream'
                self.loaded.emit(main, higher, info)
        except Cancelled:
            pass
        except Exception as e:
            record_exception('market_worker', e)
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
        self.empty_text = 'Выберите инструмент или откройте CSV'
        self._zone_labels = []
        self.fvg_settings = DisplaySettings()
        self.indicator_settings=IndicatorSettings()
        self.vwap,self.vwap_previous=[],[]
        self.fvg_plan = None
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
        cfg=self.indicator_settings
        self.fvg_settings=DisplaySettings(enabled=self.show_fvg,max_zones=cfg.fvgMaxZones,
            only_in_flow=cfg.fvgOnlyInFlow,show_lower=cfg.fvgShowM1 if analysis.timeframe=='M1' else cfg.fvgShowM5,
            higher=tuple(tf for tf,key in [('H1','showH1OnM5'),('H4','showH4OnM5'),('D1','showDOnM5'),('W1','showWOnM5'),('MN1','showMOnM5')] if getattr(cfg,key)))
        self.vwap,self.vwap_previous=vwap_series(analysis.candles,cfg.vwapAnchor,cfg.vwapSource)
        self.fvg_plan = select_fvg_display(analysis, self.parents,
                                           replace(self.fvg_settings, enabled=self.show_fvg))
        self.times = [c.time.timestamp() for c in analysis.candles]
        self.inside_bars = inside_mother_bars(analysis.candles)
        self.viewport.count = min(self.viewport.count, max(1, len(analysis.candles)))
        self.offset = 0
        self.update()

    def set_settings(self,settings):
        self.indicator_settings=settings
        for prop,key in [('show_structure','showStructure'),('show_fvg','fvgEnabled'),
                         ('show_flow','flowShowLevels'),('show_previous','flowShowPrevious')]:
            setattr(self,prop,getattr(settings,key))
        if self.analysis is not None:
            saved=self.view_state()
            self.set_analysis(self.analysis,self.parents)
            self.restore_view(saved)
        self.update()

    def format_price(self,value):
        return price_text(value,self.analysis.minimum_tick if self.analysis else .01)

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
                'end_fraction': self.viewport.bounds(len(self.times))[1] - end,
                'price_range': self.price_range, 'flow_scale': self.include_flow_in_scale,
                'right_padding': self.viewport.right_padding}

    def restore_view(self, state):
        self.viewport.count = state['count']
        self.offset = state['offset']
        if state['end'] is not None:
            ends = self.times
            end = bisect.bisect_right(ends, state['end'])
            self.offset = max(0, len(ends) - end + state.get('right_padding', 16)
                              - state.get('end_fraction', 0)) if end else 0
        self.price_range = state['price_range']
        self.include_flow_in_scale = state['flow_scale']
        self.viewport.right_padding = max(0, min(self.viewport.span - 1, state.get('right_padding', 16)))
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

    def cursor_index(self):
        if (not self.analysis or self.cursor_x is None or self.cursor_y is None
                or not self.x0 <= self.cursor_x <= self.x1 or not self.top <= self.cursor_y <= self.bottom):
            return None
        first, last = self.window()
        left, _ = self.viewport.bounds(len(self.analysis.candles))
        stride = (self.x1 - self.x0) / self.viewport.span
        index = math.floor(left + (self.cursor_x - self.x0) / max(.01, stride))
        return index if first <= index < last else None

    def axis_time_text(self):
        index = self.cursor_index()
        return candle_time_text(self.analysis.candles[index], self.analysis.timeframe) if index is not None else ''

    def wheelEvent(self, event):
        if not self.analysis:
            return
        pixels = event.pixelDelta()
        delta = event.angleDelta().y() or event.angleDelta().x() or pixels.y() or pixels.x()
        if not delta:
            return
        if event.position().x() > self.x1 or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_price(math.exp(-delta * .00165), event.position().y())
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier or event.angleDelta().x():
            self.viewport.pan(self.viewport.span * delta * .001, len(self.analysis.candles))
        else:
            self.viewport.zoom(math.exp(-delta * .00165),
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
            # Freeze the current Y scale when dragging the chart, matching
            # TradingView. Dragging the plot moves candles and ALL indicator
            # levels together rather than auto-rescaling on history changes.
            self.drag_price = self.price_range or self.last_price_range
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
                # The reference keeps the middle of the price pane fixed,
                # irrespective of where the axis was grabbed (14-18 s).
                factor = math.exp(max(-5.0, min(5.0, dy * 0.005)))
                fraction = 0.5
                anchor = (lo + hi) / 2
                new_span = max(1e-8, (hi - lo) * factor)
                self.price_range = (anchor - fraction * new_span,
                                    anchor + (1.0 - fraction) * new_span)
            elif self.drag_mode == "time_scale":
                self.viewport.count = self.drag_count
                self.viewport.offset = self.drag_offset
                self.viewport.right_padding = self.drag_padding
                # Time-scale stretching keeps the right edge fixed.
                fraction = 1.0
                factor = time_axis_zoom_factor(self.drag_origin.x() - self.x0,
                                               dx, self.x1 - self.x0)
                self.viewport.zoom(factor, fraction, len(self.analysis.candles))
            elif self.drag_mode == "pan":
                # Two-dimensional pan: horizontal translation navigates
                # candles; vertical translation moves the entire price axis
                # while preserving its scale. Both are relative to mouse
                # PRESS, so no drift accumulates while dragging.
                stride = (self.x1 - self.x0) / self.viewport.span
                drag_bars = dx / max(0.01, stride)
                self.viewport.drag_chart(drag_bars, len(self.analysis.candles),
                                         start_offset=self.drag_offset,
                                         start_padding=self.drag_padding)
                if self.drag_price is not None:
                    lo, hi = self.drag_price
                    shift = dy * (hi - lo) / max(1, self.bottom - self.top)
                    self.price_range = (lo + shift, hi + shift)
        if self.analysis:
            index = self.cursor_index()
            self.crosshair.emit(index if index is not None else -1)
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
            self.viewport.pan(len(self.analysis.candles), len(self.analysis.candles))
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
        self.crosshair.emit(-1)
        self.update()

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = dict(CHART_PALETTES[self.visual_theme])
        cfg=self.indicator_settings
        colors.update(structure_up=qcolor(cfg.upColor),structure_down=qcolor(cfg.downColor),
            flow_lines=tuple(qcolor(getattr(cfg,key)) for key in ('flowColor0','flowColor04','flowColor05','flowColor06','flowColor1')),
            previous=qcolor(cfg.flowPreviousColor))
        p.fillRect(self.rect(), colors["background"])
        if self.analysis is None or not self.analysis.candles:
            p.setPen(colors["text"])
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return
        candles = self.analysis.candles
        first, last = self.window()
        if last <= first:
            return
        self.x0, self.x1 = 64, max(100, self.width() - 88)
        self.top, self.bottom = 30, max(130, self.height() - 72)
        count = last - first
        span = max(1, self.x1 - self.x0)
        left_edge, _ = self.viewport.bounds(len(candles))
        stride = span / self.viewport.span
        def x(index: int):
            return self.x0 + (index - left_edge + 0.5) * stride
        visible = candles[first:last]
        lowest = min(b.low for b in visible)
        highest = max(b.high for b in visible)
        if cfg.vwapEnabled and self.analysis.timeframe in ('M1','M5'):
            plotted=[value for value in self.vwap[first:last] if value is not None]
            if cfg.vwapShowPrevious:
                plotted.extend(value for start,end,value in self.vwap_previous if start<last and end>=first)
            if plotted:lowest,highest=min(lowest,min(plotted)),max(highest,max(plotted))
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
            p.drawText(QRectF(self.x1 + 8, Y - 11, 80, 20), 0, self.format_price(value))
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
            for zone in self.fvg_plan.zones:
                # Source timestamps are period boundaries, not the open of
                # the breaking bar. A broken zone ends at its confirmed close.
                a = bisect.bisect_left(self.times, zone.left_time.timestamp())
                b = bisect.bisect_left(self.times, zone.right_time.timestamp())
                if b <= first or a >= last or b <= a:
                    continue
                left_x = self.x0 + (a - left_edge) * stride
                right_x = self.x0 + (b - left_edge) * stride
                self._draw_zone(p, left_x, right_x, y(zone.top), y(zone.bottom),
                    zone.side, zone.broken, zone.source_tf, stride, parent=zone.parent)
        # Structure with confirmed segments and provisional outside legs.
        if self.show_structure:
            segments=[seg for seg in self.analysis.segments if seg.confirmed][-cfg.maxSegments:]
            if cfg.showDevelopingLeg:
                segments += [seg for seg in self.analysis.segments if not seg.confirmed]
            for seg in segments:
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
                           else colors["structure_down"], cfg.structureWidth)
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
            hollow = (self.visual_theme == "tv" and cfg.ibEnabled and
                      i < len(self.inside_bars) and self.inside_bars[i])
            wick_color = (QColor("#ededed") if rising else QColor("#242424"))
            p.setPen(QPen(wick_color if self.visual_theme == "tv"
                          else (colors["wick"] or color),
                          0.9 if self.visual_theme == "tv" else 1.2))
            body_w = max(2.0, min(18.0, stride * 0.62))
            a, b = y(bar.open), y(bar.close)
            body_top, body_bottom = min(a, b), max(a, b)
            body = QRectF(center - body_w / 2, body_top,
                          body_w, max(1.3, body_bottom - body_top))
            # Standard candles draw ONLY external upper/lower wick segments.
            # A full high-to-low line would remain visible inside the
            # transparent body of an inside (hollow) candle.
            if y(bar.high) < body_top:
                p.drawLine(QPointF(center, y(bar.high)),
                           QPointF(center, body_top))
            if y(bar.low) > body_bottom:
                p.drawLine(QPointF(center, body_bottom),
                           QPointF(center, y(bar.low)))
            if not hollow:
                p.fillRect(body, color)
            if self.visual_theme == "tv":
                outline = (QColor("#e4e4e4") if rising else QColor("#474747")) if hollow else (
                    colors["candle_up_outline"] if rising else
                    colors["candle_down_outline"])
                p.setPen(QPen(outline, 1.0))
                p.drawRect(body)
        # Current and previous flow 0, 0.4, 0.5, 0.6, 1.0.
        if self.show_flow and self.analysis.flows and (self.analysis.timeframe not in ('M1','M5') or cfg.flowH1OnM5):
            st = self.analysis.flows[-1]
            if st.side and st.lo is not None and st.hi is not None:
                starts = max(first, st.start or first)
                for t, color in zip((0.0, 0.4, 0.5, 0.6, 1.0),
                                    colors["flow_lines"]):
                    price = st.lo + (st.hi - st.lo) * t
                    p.setPen(QPen(color, cfg.flowLineWidth))
                    Y = y(price)
                    right_edge=x(len(candles)-.5)
                    p.drawLine(QPointF(x(starts), Y), QPointF(right_edge, Y))
                    p.setFont(indicator_font(cfg.flowFontSize))
                    caption = (f"{t:g} ({self.format_price(price)})"
                               if self.visual_theme == "tv" else str(t))
                    if cfg.flowShowData:
                        prefix='H1 ' if self.analysis.timeframe in ('M1','M5') else ''
                        p.drawText(QRectF(right_edge - 190, Y - p.fontMetrics().height()-3, 184, p.fontMetrics().height()),
                                   int(Qt.AlignmentFlag.AlignRight), prefix+caption)
            if self.show_previous and st.previous:
                old_start, old_end, old_lo, old_hi, _ = st.previous
                if old_lo is not None and old_hi is not None:
                    for r in (0.0, 0.4, 0.5, 0.6, 1.0):
                        Y = y(old_lo + (old_hi - old_lo) * r)
                        pen = QPen(colors["previous"], cfg.flowLineWidth)
                        pen.setStyle(Qt.PenStyle.DashLine)
                        p.setPen(pen)
                        p.drawLine(QPointF(x(max(first, old_start)), Y), QPointF(x(min(last - 1, max(first, old_end))), Y))
        draw_market(self,p,x,y,first,last)
        draw_vwap(self,p,x,y,first,last)
        p.restore()
        draw_overlays(self,p)
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
                       int(Qt.AlignmentFlag.AlignCenter), self.format_price(latest.close))
        p.setPen(colors["text"])
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        hover_i = self.cursor_index()
        header_bar = candles[hover_i] if hover_i is not None else latest
        p.drawText(QRectF(self.x0, 0, max(150, self.width() - 120), 23), 0,
                   f"{self.instrument_header}   ·   {self.analysis.timeframe}   {header_bar.time.astimezone(MOSCOW):%d.%m %H:%M}   O {self.format_price(header_bar.open)}   H {self.format_price(header_bar.high)}   L {self.format_price(header_bar.low)}   C {self.format_price(header_bar.close)}")
        if self.cursor_x is not None and self.x0 <= self.cursor_x <= self.x1 and self.top <= self.cursor_y <= self.bottom:
            pen = QPen(colors["crosshair"], 0.85, Qt.PenStyle.DashLine)
            p.setPen(pen)
            snap_x = x(hover_i) if hover_i is not None else self.cursor_x
            p.drawLine(QPointF(snap_x, self.top), QPointF(snap_x, self.bottom))
            p.drawLine(QPointF(self.x0, self.cursor_y), QPointF(self.x1, self.cursor_y))
            price = lowest + (self.bottom - self.cursor_y) / max(1, self.bottom - self.top) * (highest - lowest)
            p.fillRect(QRectF(self.x1 + 3, self.cursor_y - 10, 82, 20),
                       colors["tooltip_bg"])
            p.setPen(colors["tooltip_text"])
            p.drawText(QRectF(self.x1 + 5, self.cursor_y - 10, 80, 20),
                       int(Qt.AlignmentFlag.AlignCenter), self.format_price(price))
            if hover_i is not None:
                caption = self.axis_time_text()
                tag_width = p.fontMetrics().horizontalAdvance(caption) + 14
                tag_x = max(self.x0, min(self.x1 - tag_width, snap_x - tag_width / 2))
                tag = QRectF(tag_x, self.bottom + 5, tag_width, 22)
                p.fillRect(tag, colors["tooltip_bg"])
                p.drawText(tag, int(Qt.AlignmentFlag.AlignCenter), caption)
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
        cfg=self.indicator_settings
        if self.visual_theme == "tv":
            bg=qcolor(cfg.fvgFilledColor if broken else cfg.fvgBullColor if side==1 else cfg.fvgBearColor)
            rgb=(bg.red(),bg.green(),bg.blue())
            edge=QColor(*rgb,179)
        else:
            if broken:
                bg, edge = QColor(243, 169, 45, 40), QColor(241, 173, 70, 130)
            elif side == 1:
                bg, edge = QColor(24, 196, 145, 38 if not parent else 28), QColor(19, 148, 123, 135)
            else:
                bg, edge = QColor(221, 70, 100, 38 if not parent else 28), QColor(204, 74, 95, 135)
        p.fillRect(QRectF(x0, low_y, x1 - x0, height), bg)
        p.setPen(QPen(edge, cfg.fvgBorderWidth))
        p.drawRect(QRectF(x0, low_y, x1 - x0, height))
        strong = QColor(*rgb, 255) if self.visual_theme == 'tv' else edge
        p.setPen(QPen(strong, cfg.fvgMiddleWidth, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(x0, (y0 + y1) / 2), QPointF(x1, (y0 + y1) / 2))
        if x1>x0:
            short_tf = {"D1": "D", "W1": "W", "MN1": "M"}.get(tf, tf)
            label = f"FVG {short_tf}"
            font_key={'M1':'fvgFontM1','M5':'fvgFontM5','H1':'fvgFontH1','H4':'fvgFontH4','D1':'fvgFontD','W1':'fvgFontW','MN1':'fvgFontM'}[tf]
            p.setFont(indicator_font(getattr(cfg,font_key)))
            label_height=p.fontMetrics().height()
            width=max(1,x1-x0-6)
            label_rect = QRectF(x1-width-3,low_y+(height-label_height)/2,width,label_height)
            p.setPen(strong if self.visual_theme=='tv' else CHART_PALETTES['dark']['text'])
            flags=int(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)|int(Qt.TextFlag.TextDontClip)
            p.drawText(label_rect,flags,label)


class MainWindow(StreamController, WatchlistController, QMainWindow):
    def __init__(self, service=None, *, stream_factory=None, startup=None, watchlist_service=None, preferences=None, shutdown=None, settings_store=None):
        super().__init__()
        self.setWindowTitle(f"FlowDesk {__version__} — Структура и FVG · Windows")
        self.setMinimumSize(1060, 690)
        self.resize(1420, 830)
        self.worker: MarketWorker | None = None
        self.market_service = service or MarketService(load_metadata=True)
        self.startup = service is None if startup is None else startup
        self.preferences = preferences if preferences is not None else (Preferences() if service is None else None)
        self.settings_store=settings_store or (SettingsStore() if self.startup else MemorySettingsStore())
        self.indicator_settings,self.application_settings=self.settings_store.read()
        self._applying_settings=False
        self.last_saved_context = None
        self._init_stream(stream_factory or (StreamWorker if service is None else None))
        self.pending_market = None
        self.desired_market = None
        self.loaded_market = None
        self.data_info = {}
        self.chart_views = {}
        self.csv_overrides, self.csv_provenance = {}, {}
        self.local_series, self.local_provenance = {}, {}
        self.closing_after_load = False
        self.shutdown_watchdog = shutdown
        self.source = "DEMO (синтетические свечи)"
        self.analysis: Analysis | None = None
        self.parents: dict[str, Analysis] = {}
        self.chart = CandleChart()
        self.chart.set_settings(self.indicator_settings)
        self._build_ui()
        self.auto_timer = QTimer(self)
        self.auto_timer.setSingleShot(True)
        self.auto_timer.setInterval(self.application_settings.refresh_seconds*1000)
        self.auto_timer.timeout.connect(self._auto_refresh)
        self.stream_timer = QTimer(self)
        self.stream_timer.setSingleShot(True)
        self.stream_timer.setInterval(1000)
        self.stream_timer.timeout.connect(self._flush_stream)
        self.auto_checkbox.toggled.connect(self._auto_toggled)
        self.auto_checkbox.toggled.connect(self._persist_application_controls)
        self.panel_checkbox.toggled.connect(self._persist_application_controls)
        lists = (WatchlistService() if service is None else None) if watchlist_service is None else watchlist_service
        self._init_lists(lists or None)
        if self.startup:
            selected = self.preferences.read() if self.preferences else {'symbol': 'SBER', 'board': 'TQBR', 'tf': 'H1'}
            self.symbol.setText(selected['symbol'])
            self.board.setCurrentText(selected['board'])
            self.tf.blockSignals(True)
            self.tf.setCurrentText(selected['tf'])
            self.tf.blockSignals(False)
            self.source = f"MOEX: {selected['symbol']} / {selected['board']}"
            self.status.setText(f"Загрузка {selected['symbol']}…")
            self.candle_time.setText('Свеча: — · загрузка данных MOEX')
            self.chart.empty_text = f"Загрузка {selected['symbol']} с MOEX…"
            QTimer.singleShot(0, self._startup_load)
        else:
            self._show_data(analyze(demo_data(), "H1",settings=self.indicator_settings), {}, self.source)

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
        self.board.addItems(["TQBR", "RFUD", "TQTF", "TQTD", "SPEQ", "TQOB"])
        self.board.setToolTip("Режим торгов MOEX (для акций обычно TQBR)")
        header.addWidget(self.board)
        self.symbol = QLineEdit("SBER")
        self.symbol.setFixedWidth(110)
        self.symbol.setPlaceholderText("Тикер · Enter")
        header.addWidget(self.symbol)
        self.symbol.returnPressed.connect(self.load_moex)
        self.tf = TimeframeBar()
        header.addWidget(self.tf)
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
        self.settings_button=QPushButton('⚙')
        self.settings_button.setObjectName('settingsButton')
        self.settings_button.setAccessibleName('Настройки приложения')
        self.settings_button.setToolTip('Настройки приложения · эталон v2.11')
        self.settings_button.setFixedWidth(42)
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
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
        self.auto_checkbox = QCheckBox("Авто")
        self.auto_checkbox.setChecked(self.application_settings.auto_updates)
        self.auto_checkbox.setToolTip(self._auto_help())
        navigation.addWidget(self.auto_checkbox)
        navigation.addSpacing(10)
        self.panel_checkbox = QCheckBox("Панель")
        self.panel_checkbox.setChecked(self.application_settings.show_panel)
        self.panel_checkbox.setToolTip("Показать диагностику, последние FVG и события")
        navigation.addWidget(self.panel_checkbox)
        navigation.addStretch()
        self.data_age = QLabel('')
        layout.addLayout(navigation)
        self.banner = QLabel("ПРЕДВАРИТЕЛЬНЫЕ РАСЧЁТЫ • совпадение с TradingView не подтверждено • без выставления заявок")
        self.banner.setObjectName("warning")
        layout.addWidget(self.banner)
        self.feed_label = QLabel('')
        self.feed_label.setWordWrap(True)
        self.feed_label.hide()
        layout.addWidget(self.feed_label)
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
        self.display_checkboxes={}
        for label, prop in [
            ("Структурная ломаная", "show_structure"),
            ("FVG и зоны старших ТФ", "show_fvg"),
            ("Уровни потока 0–1", "show_flow"),
            ("Предыдущий поток", "show_previous"),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(getattr(self.chart,prop))
            self.display_checkboxes[prop]=cb
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
        self.right_panel.setVisible(self.application_settings.show_panel)
        splitter.addWidget(self._build_watchlist())
        splitter.setSizes([1090, 0, 260])
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.candle_time = QLabel('Свеча: —')
        footer.addWidget(self.data_age)
        footer.addStretch()
        footer.addWidget(self.candle_time)
        layout.addLayout(footer)
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
        mapping={'show_structure':'showStructure','show_fvg':'fvgEnabled','show_flow':'flowShowLevels','show_previous':'flowShowPrevious'}
        self._apply_settings(self.indicator_settings.changed(**{mapping[key]:value}),self.application_settings)

    def open_settings(self):
        dialog=SettingsDialog(self.indicator_settings,self.application_settings,self)
        dialog.applied.connect(self._apply_settings)
        dialog.exec()

    def _auto_help(self):
        return (f'Поток MOEX ISS+ DEMO. Резерв — HTTP через {self.application_settings.refresh_seconds} с. '
                'Внутридневные свечи и котировки могут поступать с задержкой около 15 минут.')

    def _persist_application_controls(self,*_):
        if self._applying_settings:return
        self.application_settings=replace(self.application_settings,auto_updates=self.auto_checkbox.isChecked(),show_panel=self.panel_checkbox.isChecked())
        try:self.settings_store.write(self.indicator_settings,self.application_settings)
        except OSError as error:
            record_exception('settings_save',error)
            self.status.setText(f'Не удалось сохранить настройки: {error}')

    def _apply_settings(self,indicator,application):
        recalculate=indicator.calculation_key()!=self.indicator_settings.calculation_key()
        self.indicator_settings,self.application_settings=indicator,application
        try:self.settings_store.write(indicator,application)
        except OSError as error:
            record_exception('settings_save',error)
            QMessageBox.warning(self,'Настройки',f'Настройки применены, но не сохранены: {error}')
        self._applying_settings=True
        try:
            self.chart.set_settings(indicator)
            for prop,cb in self.display_checkboxes.items():
                cb.blockSignals(True)
                cb.setChecked(getattr(self.chart,prop))
                cb.blockSignals(False)
            self.auto_timer.setInterval(application.refresh_seconds*1000)
            self.auto_checkbox.setToolTip(self._auto_help())
            self.auto_checkbox.setChecked(application.auto_updates)
            self.panel_checkbox.setChecked(application.show_panel)
        finally:self._applying_settings=False
        if recalculate and self.source.startswith('MOEX:'):
            context=self.desired_market or self.loaded_market
            if context:
                request=(*context,False)
                if self.worker is not None:self.pending_market=request
                else:self._start_market(request)
        elif recalculate and self.analysis:
            series={tf:p.candles for tf,p in self.parents.items()}
            series[self.analysis.timeframe]=self.analysis.candles
            updated,parents=analyze_timeframes(series,self.analysis.timeframe,asof=self.analysis.asof,settings=indicator)
            self._show_data(updated,parents,self.source)
        elif self.analysis:
            self._show_data(self.analysis,self.parents,self.source)
        self._schedule_auto_refresh()
        self._ensure_stream()

    def load_moex(self):
        if self.closing_after_load:
            return
        self.stream_disabled_key = None
        self._request_market(refresh=True)

    def _startup_load(self):
        if (self.closing_after_load or self.worker is not None or self.analysis is not None
                or not self.auto_checkbox.isChecked()):
            return
        context = (self.symbol.text().strip().upper(), self.board.currentText(), self.tf.currentText())
        self.desired_market = context
        self._start_market((*context, False), automatic=True)

    def _schedule_auto_refresh(self, *_):
        if (self.auto_checkbox.isChecked() and self.loaded_market and
                self.source.startswith('MOEX:') and self.worker is None and not self.closing_after_load):
            if not self.auto_timer.isActive():
                self.auto_timer.start()
        else:
            self.auto_timer.stop()

    def _auto_refresh(self):
        if (not self.auto_checkbox.isChecked() or not self.loaded_market or
                not self.source.startswith('MOEX:') or self.worker is not None or self.closing_after_load):
            return
        # Never open an unsubmitted ticker/board or replace a queued user request.
        context = (self.symbol.text().strip().upper(), self.board.currentText(), self.tf.currentText())
        if context != self.loaded_market:
            self._schedule_auto_refresh()
            return
        confirmation = self.data_info.get('next_confirmation_at')
        boundary_due = bool(confirmation and datetime.now(timezone.utc) >= datetime.fromisoformat(confirmation))
        if self.stream_ready and not self.stream_needs_repair and self.stream_worker and not boundary_due:
            self._schedule_auto_refresh()
            return
        self.desired_market = self.loaded_market
        self._start_market((*self.loaded_market, True), automatic=True,
                           repair=bool(self.stream_worker and (self.stream_needs_repair or boundary_due)))

    def _timeframe_changed(self, tf):
        if self.source.startswith('MOEX:') or self.worker is not None:
            self._request_market(refresh=False)
        elif self.analysis and tf != self.analysis.timeframe:
            series = self.local_series or {self.analysis.timeframe: self.analysis.candles,
                                           **{t: a.candles for t, a in self.parents.items()}}
            if tf in series:
                provenance = self.local_provenance or {self.analysis.timeframe: self.analysis.session_info,
                                                       **{t: a.session_info for t, a in self.parents.items()}}
                a, parents = analyze_timeframes(series, tf, asof=self.analysis.asof, provenance=provenance,settings=self.indicator_settings)
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
        if self.stream_key and self.stream_key != request[:2]:
            self._stop_stream()
        self.desired_market = request[:3]
        if self.worker is not None:
            self.pending_market = request
            self.status.setText(f'Следующий график: {symbol} · {request[2]}; завершение текущей загрузки')
            return
        self._start_market(request)

    def _start_market(self, request, *, automatic=False, stream_batch=(), repair=False):
        if not stream_batch:
            self.auto_timer.stop()
        symbol, board, tf, refresh = request
        if self.loaded_market is None or self.loaded_market[:2] != request[:2]:
            self.csv_overrides, self.csv_provenance = {}, {}
        self.export_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        self.h1_button.setEnabled(False)
        self.status.setText(f'Открытие {symbol} · {tf}…')
        self.worker = MarketWorker(symbol, tf, board, self.market_service, refresh=refresh,
                                   overrides=dict(self.csv_overrides), provenance=dict(self.csv_provenance),
                                   stream_batch=stream_batch, repair=repair,settings=self.indicator_settings)
        self.worker.automatic = automatic
        self.worker.stream_epoch = self.stream_epoch
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(self._failed)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self._market_finished)
        self.worker.start()

    def _loaded(self, analysis: Analysis, higher: dict, info: dict):
        context = (info['symbol'], info['board'], info['tf'])
        if context != self.desired_market or self.closing_after_load:
            return
        if (getattr(self.worker,'settings',None) is not None and
            self.worker.settings.calculation_key()!=self.indicator_settings.calculation_key()):
            self.pending_market=(*self.desired_market,False)
            return
        self.loaded_market = context
        if self.preferences and context != self.last_saved_context:
            self.preferences.write(*context)
            self.last_saved_context = context
        self._highlight_instrument()
        self.data_info = info
        if (self.worker.repair and self.worker.stream_epoch == self.stream_epoch and
                context[:2] == self.stream_key and info.get('tier') == 'network'):
            self.stream_needs_repair = False
        if info.get('stream_repair_required'):
            self.stream_needs_repair = True
            if self.worker.stream_batch:
                self.stream_repair_pending = True
        self._show_data(analysis, higher, f"MOEX: {info['symbol']} / {info['board']}")

    def _failed(self, error: str):
        if self.startup and self.analysis is None and getattr(self.worker, 'automatic', False) and not self.closing_after_load:
            QTimer.singleShot(30000, self._startup_load)
        if getattr(self.worker, 'stream_batch', ()) and error.startswith('StreamGap:'):
            self.stream_needs_repair = self.stream_repair_pending = True
        elif getattr(self.worker, 'repair', False) or getattr(self.worker, 'stream_batch', ()):
            # If minute priming fails, keep the original HTTP-only path usable.
            self.stream_disabled_key = self.stream_key
            self._stop_stream()
            self.stream_state = 'HTTP · восстановление потока недоступно'
            self._feed_text()
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
        for control in (self.csv_button, self.h1_button, self.export_button):
            control.setEnabled(True)
        if self.analysis:
            self.tf.blockSignals(True)
            self.tf.setCurrentText(self.analysis.timeframe)
            self.tf.blockSignals(False)
            if self.loaded_market and not automatic:
                self.symbol.setText(self.loaded_market[0])
                self.board.setCurrentText(self.loaded_market[1])
        self._schedule_auto_refresh()
        self._ensure_stream()
        if self.stream_buffer or self.stream_repair_pending:
            self.stream_timer.start()

    def closeEvent(self, event):
        if self.shutdown_watchdog is not None:
            self.shutdown_watchdog.arm()
        self.closing_after_load = True
        self.pending_market = None
        self.auto_timer.stop()
        self._stop_stream()
        self.list_timer.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
        if self.list_worker is not None:
            self.list_worker.requestInterruption()
        if self.worker is not None or self.retiring_streams or self.list_worker is not None:
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
            self._stop_stream()
            self.local_series = {tf: candles}
            self.local_provenance = {tf: {'mode': 'USER_CSV', 'file': Path(path).name}}
            # Never apply a prior H1 CSV from another instrument to a newly
            # opened lower-TF CSV. User explicitly imports the matching H1.
            analysis, _ = analyze_timeframes({tf: candles}, tf,settings=self.indicator_settings)
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
            updated, parents = analyze_timeframes(series, main.timeframe, asof=main.asof, provenance=provenance,settings=self.indicator_settings)
            if self.source.startswith('MOEX:'):
                self._stop_stream()
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
            self.feed_label.hide()
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
        self._candle_timestamp(self.chart.cursor_index())
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
        zones = self.chart.fvg_plan.zones
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
        self.status.setText(f"{source} · {len(analysis.candles)} свечей ({analysis.confirmed_bars} закрытых) · FVG: {len(zones)} показано ({len(analysis.fvg)} своих в расчёте){session_desc} · {analysis.flow_source_note} · {coverage} · НЕ сверено с Pine")
        if self.data_info:
            observed = datetime.fromisoformat(self.data_info['asof']).astimezone(MOSCOW)
            origin = {'memory': 'Сохранённые данные', 'disk': 'Сохранённые данные',
                      'offline': 'Офлайн', 'network': 'Обновлено', 'stream': 'Поток'}[self.data_info['tier']]
            requested = datetime.fromisoformat(self.data_info.get('requested_at', self.data_info['asof'])).astimezone(MOSCOW)
            if self.data_info.get('stream_received_at') and self.data_info['tier'] == 'stream':
                requested = datetime.fromisoformat(self.data_info['stream_received_at']).astimezone(MOSCOW)
            self.data_age.setText(f'MOEX ISS · {origin}')
            self.data_age.setToolTip(f'Получение / запрос: {requested:%d.%m.%Y %H:%M:%S} МСК\nРасчёт закрытых свечей до: {observed:%d.%m.%Y %H:%M:%S} МСК')
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
            self._candle_timestamp()
            return
        self._candle_timestamp(i)
        bar = self.analysis.candles[i]
        flow = self.analysis.flows[i]
        dirtext = "LONG" if flow.side == 1 else "SHORT" if flow.side == -1 else "WAIT"
        h1_info = (f" · H1 закрыта {flow.source_confirmed_at.strftime('%d.%m %H:%M')}" if flow.source_confirmed_at else "")
        self.hover_info.setText(f"{bar.time.strftime('%d.%m.%Y %H:%M')} · O {short_number(bar.open)} · H {short_number(bar.high)} · L {short_number(bar.low)} · C {short_number(bar.close)} · {dirtext}{h1_info}")

    def _candle_timestamp(self, index=None):
        if not self.analysis or not self.analysis.candles:
            self.candle_time.setText('Свеча: —')
            return
        bar = self.analysis.candles[-1 if index is None else index]
        stamp = bar.time.astimezone(MOSCOW)
        title = 'Начало последней свечи' if index is None else 'Начало свечи под курсором'
        self.candle_time.setText(f'{title}: {candle_time_text(bar, self.analysis.timeframe)}')
        end = candle_close_time(bar, self.analysis.timeframe).astimezone(MOSCOW)
        self.candle_time.setToolTip(f'Биржевое начало свечи: {stamp:%d.%m.%Y %H:%M:%S} МСК\nКонец периода: {end:%d.%m.%Y %H:%M:%S} МСК')

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
                "reference_version":"2.11",
                "indicator_settings":self.indicator_settings.as_dict(),
                "application_settings":vars(self.application_settings),
                "last_runtime_error": last_exception(),
                "fvg_display": self.chart.fvg_plan.as_dict(),
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
                        "source": ("MOEX_NATIVE_CANDLES" if p.session_info.get("native_higher") else "MOEX_RFUD_NATIVE_H1"
                                   if p.session_info.get("market") == 'futures' else "MOEX_CLEAN_H1_PHYSICAL_CALENDAR"
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
    shutdown = ShutdownWatchdog()
    app.aboutToQuit.connect(shutdown.arm)
    window = MainWindow(shutdown=shutdown)
    window.show()
    return app.exec()

