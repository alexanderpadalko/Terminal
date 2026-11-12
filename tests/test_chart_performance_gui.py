"""Mouse hover must not redraw history; cached and full images must agree."""
import os
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtCore import QEvent,QPointF,Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import CandleChart
    QT=True
except ImportError:
    if os.environ.get('CI')=='true':raise
    QT=False
from flowdesk.engine import Analysis,FlowState,Segment
from flowdesk.market_data import Candle,MOSCOW


def large_frame(count=12000):
    base=datetime(2025,1,1,10,tzinfo=MOSCOW)
    bars=[Candle(base+timedelta(hours=i),100,102,98,101,100) for i in range(count)]
    segments=[Segment(i,99,i+1,102,1) for i in range(count-1)]
    return Analysis(bars,segments,[],[FlowState(i,1,98,102,0) for i in range(count)],[],'H1',
                    asof=bars[-1].time+timedelta(hours=1))


@unittest.skipUnless(QT,'Windows Qt CI')
class TestChartPerformanceGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def chart(self):
        chart=CandleChart();chart.resize(1100,720)
        chart.set_analysis(large_frame())
        chart.show();self.app.processEvents();chart.grab()
        self.addCleanup(chart.close)
        return chart

    def test_hover_reuses_surface_and_notifies_only_on_candle_change(self):
        chart=self.chart();chart.leaveEvent(None)
        events=[];chart.crosshair.connect(events.append)
        x=(chart.x0+chart.x1)/2
        with patch.object(chart,'_render_surface',wraps=chart._render_surface) as render:
            for i in range(25):
                event=QMouseEvent(QEvent.Type.MouseMove,QPointF(x,chart.top+30+i),
                                  Qt.MouseButton.NoButton,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier)
                chart.mouseMoveEvent(event);chart.grab()
            self.assertEqual(render.call_count,0)
        self.assertEqual(len(events),1)

    def test_hover_pixels_equal_forced_complete_repaint(self):
        chart=self.chart()
        chart.cursor_x=(chart.x0+chart.x1)/2;chart.cursor_y=(chart.top+chart.bottom)/2
        cached=chart.grab().toImage()
        chart._surface_key=None
        full=chart.grab().toImage()
        self.assertEqual(bytes(cached.constBits()),bytes(full.constBits()))

    def test_pan_price_zoom_and_settings_rebuild_surface(self):
        chart=self.chart()
        with patch.object(chart,'_render_surface',wraps=chart._render_surface) as render:
            chart.viewport.pan(3,len(chart.analysis.candles));chart.grab()
            self.assertEqual(render.call_count,1)
            chart.price_range=(90,110);chart.grab()
            self.assertEqual(render.call_count,2)
            chart.set_settings(chart.indicator_settings.changed(showStructure=False));chart.grab()
            self.assertEqual(render.call_count,3)
