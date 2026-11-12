import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import CandleChart, MainWindow
    from test_gui_navigation import FakeMarket
    QT = True
except ImportError:
    if os.environ.get('CI') == 'true': raise
    QT = False

from test_fvg_display import child, lower, source


@unittest.skipUnless(QT, 'Qt runs in Windows CI')
class TestFvgDisplayGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_qt_draws_only_the_selected_parent_and_eligible_children(self):
        a = lower([child(), child(bottom=200, top=210, start=30), child(start=50, broken=70)])
        chart = CandleChart()
        chart.resize(1100, 720)
        chart.set_analysis(a, {'H1': source()})
        chart.show_structure = chart.show_flow = False
        with patch.object(chart, '_draw_zone', wraps=chart._draw_zone) as draw:
            chart.show(); self.app.processEvents()
            chart.grab()
            self.assertEqual([(c.args[7], c.args[6]) for c in draw.call_args_list],
                             [('H1', False), ('M1', True), ('M1', False)])
        self.assertEqual(len(a.fvg), 3)
        chart.close()

    def test_diagnostic_table_uses_display_plan_not_unfiltered_archive(self):
        a = lower([child(), child(bottom=200, top=210, start=30)])
        w = MainWindow(service=FakeMarket(), watchlist_service=False)
        w._show_data(a, {'H1': source()}, 'CSV: FVG display fixture')
        self.assertEqual(w.zones_table.rowCount(), 2)
        self.assertIn('2 показано', w.status.text())
        self.assertEqual(len(a.fvg), 2)
        data = w.chart.fvg_plan.as_dict()
        self.assertEqual(data['working_parents'], 1)
        self.assertEqual([z['source_tf'] for z in data['zones']], ['H1', 'M1'])
        w._toggle('show_fvg', False)
        self.assertEqual(w.zones_table.rowCount(), 0)
        self.assertEqual(w.chart.fvg_plan.zones, ())
        w._toggle('show_fvg', True)
        self.assertEqual(w.zones_table.rowCount(), 2)
        w.close()
