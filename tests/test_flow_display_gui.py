import os
import unittest
from datetime import timedelta

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import CandleChart
    QT=True
except ImportError:
    if os.environ.get('CI')=='true':raise
    QT=False
import test_flow_display as fixtures
from flowdesk.settings import IndicatorSettings


@unittest.skipUnless(QT,'Windows Qt CI')
class TestFlowDisplayGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_previous_flow_outside_loaded_history_does_not_leave_edge_pixels(self):
        analysis,parent=fixtures.TestFlowDisplay().fixture()
        for state in parent.flows:
            state.previous_start_at=fixtures.BASE-timedelta(days=3)
            state.previous_end_at=fixtures.BASE-timedelta(days=2)
        chart=CandleChart()
        chart.resize(1100,720)
        cfg=IndicatorSettings().changed(flowDiagnosticEnabled=False,currentFvgTableEnabled=False,fvgEnabled=False)
        chart.set_settings(cfg)
        chart.set_analysis(analysis,{'H1':parent})
        chart.show();self.app.processEvents()
        before=chart.grab().toImage()
        chart.set_settings(cfg.changed(flowShowPrevious=False))
        after=chart.grab().toImage()
        self.assertEqual(bytes(before.constBits()),bytes(after.constBits()))
        chart.close()

    def test_chart_and_gear_use_the_same_source_for_levels_and_diagnostic(self):
        analysis,parent=fixtures.TestFlowDisplay().fixture()
        chart=CandleChart()
        chart.resize(1100,720)
        chart.set_settings(IndicatorSettings().changed(flowDiagnosticEnabled=True))
        chart.set_analysis(analysis,{'H1':parent})
        chart.show();self.app.processEvents()
        self.assertIn('№3:',chart.flow_plan.diagnostic)
        chart.set_settings(chart.indicator_settings.changed(flowH1OnM5=False))
        self.assertEqual(chart.flow_plan.levels,())
        self.assertIn('№3:',chart.flow_plan.diagnostic)
        self.assertFalse(chart.grab().isNull())
        chart.close()
