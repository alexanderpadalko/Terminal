import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication,QDialogButtonBox
    from flowdesk.gui import MainWindow
    from flowdesk.settings_dialog import SettingsDialog
    QT=True
except ImportError:
    if os.environ.get('CI')=='true':raise
    QT=False
from flowdesk.settings import IndicatorSettings,ApplicationSettings,SettingsStore


@unittest.skipUnless(QT,'Windows Qt CI')
class TestSettingsGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_dialog_exposes_all_74_values_and_cancel_does_not_apply(self):
        dialog=SettingsDialog(IndicatorSettings(),ApplicationSettings())
        self.assertEqual(len(dialog.controls),74)
        self.assertEqual(dialog.tabs.count(),7)
        applied=[]
        dialog.applied.connect(lambda *values:applied.append(values))
        dialog.controls['fvgMaxZones'].setValue(7)
        dialog.reject()
        self.assertEqual(applied,[])

    def test_gear_apply_restart_restores_values_and_render_stays_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder)/'settings.json')
            window=MainWindow(startup=False,watchlist_service=False,settings_store=store)
            window.resize(1420,850)
            window.show()
            self.app.processEvents()
            self.assertIn('v2.11',window.settings_button.toolTip())
            dialog=SettingsDialog(window.indicator_settings,window.application_settings,window)
            dialog.applied.connect(window._apply_settings)
            dialog.controls['fvgMaxZones'].setValue(7)
            dialog.controls['showH4OnM5'].setChecked(False)
            dialog.controls['flowShowData'].setChecked(True)
            dialog.controls['fvgFontM'].setCurrentText('small')
            dialog.controls['flowDiagnosticEnabled'].setChecked(True)
            dialog.controls['flowDiagnosticFont'].setCurrentText('small')
            dialog.apply()
            self.app.processEvents()
            self.assertEqual(window.chart.fvg_settings.max_zones,7)
            self.assertNotIn('H4',window.chart.fvg_settings.higher)
            self.assertTrue(window.indicator_settings.flowShowData)
            self.assertFalse(window.grab().isNull())
            window.close()
            other=MainWindow(startup=False,watchlist_service=False,settings_store=SettingsStore(store.path))
            self.assertEqual(other.indicator_settings.fvgMaxZones,7)
            self.assertTrue(other.indicator_settings.flowDiagnosticEnabled)
            other.close()
            dialog.close()

    def test_defaults_reset_changes_fields_until_apply(self):
        changed=IndicatorSettings().changed(fvgMaxZones=3,flowShowData=True)
        dialog=SettingsDialog(changed,ApplicationSettings(False,120,True))
        dialog.restore_defaults()
        indicator,application=dialog.values()
        self.assertEqual(indicator.as_dict(),IndicatorSettings().as_dict())
        self.assertEqual(application,ApplicationSettings())
        dialog.close()
