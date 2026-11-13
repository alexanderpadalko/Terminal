import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication,QDialogButtonBox,QLabel
    from PySide6.QtGui import QImage,QPainter,QColor
    from flowdesk.gui import MainWindow,CandleChart
    from flowdesk.settings_dialog import SettingsDialog,ColorButton,palette_colors,opacity_percent,rgba_value
    QT=True
except ImportError:
    if os.environ.get('CI')=='true':raise
    QT=False
from flowdesk.settings import IndicatorSettings,ApplicationSettings,SettingsStore,CHART_COLOR_FIELDS,CHART_THEME_DEFAULTS


@unittest.skipUnless(QT,'Windows Qt CI')
class TestSettingsGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_dialog_exposes_73_editable_values_and_cancel_does_not_apply(self):
        dialog=SettingsDialog(IndicatorSettings(),ApplicationSettings())
        self.assertEqual(len(dialog.controls),73)
        self.assertNotIn("showDevelopingLeg",dialog.controls)
        self.assertEqual(dialog.windowTitle(),"Настройки FlowDesk")
        self.assertFalse(any("Эталон" in widget.text() or
                             "TradingView" in widget.text()
                             for widget in dialog.findChildren(QLabel)))
        self.assertEqual(dialog.tabs.count(),8)
        self.assertEqual(dialog.tabs.tabText(0),"Настройки графика")
        self.assertEqual(dialog.tabs.tabText(1),"Структурное движение")
        self.assertEqual(len(dialog.chart_controls),8)
        self.assertFalse(any("TradingView" in control.toolTip()
                             for control in dialog.controls.values()))
        applied=[]
        dialog.applied.connect(lambda *values:applied.append(values))
        dialog.controls['fvgMaxZones'].setValue(7)
        dialog.reject()
        self.assertEqual(applied,[])

    def test_palette_swatch_and_opacity_persist_with_hex_alpha(self):
        dialog=SettingsDialog(IndicatorSettings(),ApplicationSettings())
        dialog.show()
        self.app.processEvents()
        button=dialog.controls["upColor"]
        self.assertIsInstance(button,ColorButton)
        self.assertEqual(sum(map(len,palette_colors())),70)
        button.choose()
        self.app.processEvents()
        popup=button._palette
        self.assertIsNotNone(popup)
        self.assertEqual(len(popup.swatches),70)
        popup._apply_rgb("#123456")
        self.assertEqual(button.value,"#123456FF")
        popup.transparency.setValue(40)
        self.assertEqual(button.value,"#12345699")
        self.assertEqual(popup.percent.value(),40)
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder)/"colors.json")
            store.write(dialog.values()[0],ApplicationSettings())
            loaded,_=SettingsStore(store.path).read()
            self.assertEqual(loaded.upColor,"#12345699")
        popup.percent.setValue(100)
        self.assertEqual(button.value,"#12345600")
        popup.percent.setValue(0)
        self.assertEqual(button.value,"#123456FF")
        with patch("flowdesk.settings_dialog.QColorDialog.getColor",
                   return_value=QColor("#A1B2C3")):
            popup._custom_color()
        self.assertEqual(button.value,"#A1B2C3FF")
        self.assertEqual(dialog.values()[0].upColor,"#A1B2C3FF")
        self.assertEqual(opacity_percent("#00000080"),50)
        self.assertEqual(rgba_value("#ABCDEF",75),"#ABCDEF40")
        popup.hide()
        dialog.restore_defaults()
        self.assertEqual(dialog.values()[0].upColor,IndicatorSettings().upColor)
        dialog.close()

    def test_chart_colors_save_reload_and_display_after_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder)/'chart-colors.json')
            dlg=SettingsDialog(IndicatorSettings(),ApplicationSettings())
            try:
                dlg.chart_controls['chart_candle_up'].set_value('#118844FF')
                dlg.chart_controls['chart_background'].set_value('#202A36FF')
                dlg.chart_controls['chart_grid'].set_value('#BBCCDD55')
                indicator,application=dlg.values()
                store.write(indicator,application)
                _,loaded=SettingsStore(store.path).read()
                self.assertEqual(loaded.chart_candle_up,'#118844FF')
                self.assertEqual(loaded.chart_background,'#202A36FF')
                self.assertEqual(loaded.chart_grid,'#BBCCDD55')
                chart=CandleChart()
                chart.resize(850,570)
                chart.set_chart_colors(loaded)
                chart.show()
                self.app.processEvents()
                self.assertEqual(chart.grab().toImage().pixelColor(3,3).name(),
                                 '#202a36')
                chart.close()
            finally:
                dlg.close()

    def test_old_settings_without_chart_colors_are_backwards_compatible(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SettingsStore(Path(folder)/'old.json')
            store.write(IndicatorSettings(),ApplicationSettings())
            import json
            payload=json.loads(store.path.read_text(encoding='utf-8'))
            for field,_ in CHART_COLOR_FIELDS:
                payload['application'].pop(field,None)
            store.path.write_text(json.dumps(payload),encoding='utf-8')
            _,settings=SettingsStore(store.path).read()
            for field,_ in CHART_COLOR_FIELDS:
                self.assertIsNone(getattr(settings,field))
            with self.assertRaises(ValueError):
                ApplicationSettings(chart_grid='#invalid')

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
            dialog.refresh.setValue(120)
            dialog.apply()
            self.app.processEvents()
            self.assertEqual(window.chart.fvg_settings.max_zones,7)
            self.assertNotIn('H4',window.chart.fvg_settings.higher)
            self.assertTrue(window.indicator_settings.flowShowData)
            self.assertEqual(window.auto_timer.interval(),120000)
            self.assertIn('120',window.auto_checkbox.toolTip())
            self.assertFalse(window.grab().isNull())
            window.close()
            other=MainWindow(startup=False,watchlist_service=False,settings_store=SettingsStore(store.path))
            self.assertEqual(other.indicator_settings.fvgMaxZones,7)
            self.assertTrue(other.indicator_settings.flowDiagnosticEnabled)
            self.assertEqual(other.application_settings.refresh_seconds,120)
            other.close()
            dialog.close()

    def test_thin_monthly_fvg_keeps_full_size_text_outside_its_height(self):
        chart=CandleChart()
        canvas=QImage(320,110,QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QColor('#898989'))
        painter=QPainter(canvas)
        chart._draw_zone(painter,60,260,50,52,1,False,'MN1',10,parent=True)
        painter.end()
        # Pine box text overflows a narrow zone; clipping to its two-pixel
        # height would erase the entire upper part of the huge monthly label.
        dark=sum(canvas.pixelColor(x,y).red()<80 and canvas.pixelColor(x,y).green()<100
                 for y in range(30,48) for x in range(80,270))
        self.assertGreater(dark,20)
        chart.close()

    def test_defaults_reset_changes_fields_until_apply(self):
        changed=IndicatorSettings().changed(fvgMaxZones=3,flowShowData=True)
        dialog=SettingsDialog(changed,ApplicationSettings(False,120,True))
        dialog.restore_defaults()
        indicator,application=dialog.values()
        self.assertEqual(indicator.as_dict(),IndicatorSettings().as_dict())
        self.assertEqual((application.auto_updates,application.refresh_seconds,
                          application.show_panel,application.allow_http),
                         (True,60,False,False))
        for field,_ in CHART_COLOR_FIELDS:
            self.assertEqual(getattr(application,field),CHART_THEME_DEFAULTS['tv'][field])
        dialog.close()
