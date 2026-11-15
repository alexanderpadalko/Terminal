import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication,QDialogButtonBox,QLabel,QAbstractSpinBox
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
        self.assertEqual(len(dialog.controls),71)
        for obsolete in ("showDevelopingLeg","showMarketStatusInput",
                         "fvgMtfDiagnostics"):
            self.assertNotIn(obsolete,dialog.controls)
        self.assertEqual(dialog.windowTitle(),"Настройки FlowDesk")
        self.assertFalse(any("Эталон" in widget.text() or
                             "TradingView" in widget.text()
                             for widget in dialog.findChildren(QLabel)))
        self.assertEqual(dialog.tabs.count(),8)
        self.assertEqual(dialog.tabs.tabText(0),"Настройки графика")
        self.assertEqual(dialog.tabs.tabText(1),"Структурное движение")
        self.assertEqual(len(dialog.chart_controls),9)
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
        self.assertEqual(len(palette_colors()),8)
        self.assertTrue(all(len(row)==10 for row in palette_colors()))
        self.assertEqual(sum(map(len,palette_colors())),80)
        self.assertEqual(palette_colors()[1][4],"#089981")
        self.assertEqual(palette_colors()[6][4],"#056656")
        self.assertEqual(palette_colors()[7][9],"#880E4F")
        button.choose()
        self.app.processEvents()
        popup=button._palette
        self.assertIsNotNone(popup)
        self.assertEqual(len(popup.swatches),80)
        self.assertEqual(popup.percent.buttonSymbols(),
                         QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.assertGreaterEqual(popup.percent.width(),90)
        self.assertGreaterEqual(popup.width(),300)
        # The full "100%" is legible within the editable area at native DPI.
        popup.percent.setValue(100)
        self.app.processEvents()
        self.assertEqual(popup.percent.text(),"100%")
        # Offscreen CI can report a 1px unpolished QLineEdit despite the
        # parent spin box being 90px; assert the actual allocated control
        # and no native arrow overlay instead.
        needed=popup.percent.fontMetrics().horizontalAdvance("100%")
        self.assertGreater(popup.percent.width(),needed+16)
        popup.percent.setValue(0)
        # Initially the original up-color is a real swatch; it is marked.
        selected=[swatch for swatch,rgb in popup.swatches if swatch.isChecked()]
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0].rgb,"#056656")
        self.assertFalse(popup.custom_button.isChecked())
        popup._apply_rgb("#089981")
        self.assertEqual([swatch.rgb for swatch,_ in popup.swatches
                          if swatch.isChecked()],["#089981"])
        self.assertFalse(popup.custom_button.isChecked())
        popup._apply_rgb("#123456")
        self.assertFalse(any(swatch.isChecked() for swatch,_ in popup.swatches))
        self.assertTrue(popup.custom_button.isChecked())
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
        self.assertTrue(popup.custom_button.isChecked())
        # Selection marker has a white outer ring, even on dark colors.
        popup._apply_rgb("#056656")
        self.app.processEvents()
        selected_cell=next(cell for cell,rgb in popup.swatches if rgb=="#056656")
        self.assertTrue(selected_cell.isChecked())
        self.assertGreater(selected_cell.grab().toImage().pixelColor(1,11).red(),185)
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
                dlg.chart_controls['chart_axis_text'].set_value('#F0E0D0FF')
                indicator,application=dlg.values()
                store.write(indicator,application)
                _,loaded=SettingsStore(store.path).read()
                self.assertEqual(loaded.chart_candle_up,'#118844FF')
                self.assertEqual(loaded.chart_background,'#202A36FF')
                self.assertEqual(loaded.chart_grid,'#BBCCDD55')
                self.assertEqual(loaded.chart_axis_text,'#F0E0D0FF')
                chart=CandleChart()
                chart.resize(850,570)
                chart.set_chart_colors(loaded)
                from flowdesk.engine import analyze
                from flowdesk.gui import demo_data
                chart.set_analysis(analyze(demo_data(),"H1"))
                chart.show()
                self.app.processEvents()
                image=chart.grab().toImage()
                self.assertEqual(image.pixelColor(3,3).name(),'#202a36')
                self.assertEqual(chart._surface_context[0]["axis_text"].name(),
                                 '#f0e0d0')
                # Price/time tick labels both use the configured text color.
                def light_pixels(bounds):
                    x0,y0,x1,y1=bounds
                    return sum(image.pixelColor(x,y).red()>195 and
                               image.pixelColor(x,y).green()>165 and
                               image.pixelColor(x,y).blue()>145
                               for y in range(y0,y1,2) for x in range(x0,x1,2))
                self.assertGreater(light_pixels(
                    (chart.x1+8,chart.top,chart.width()-3,chart.bottom)),0)
                self.assertGreater(light_pixels(
                    (chart.x0,chart.bottom+10,chart.x1,chart.bottom+34)),0)
                chart.close()
            finally:
                dlg.close()

    def test_single_user_theme_fvg_colors_persist_in_finam_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder)/"finam-user-theme.json")
            dialog = SettingsDialog(IndicatorSettings(), ApplicationSettings(),
                                    finam_only=True)
            try:
                chosen = {"fvgBullColor": "#1246AB99",
                          "fvgBearColor": "#ED289577",
                          "fvgFilledColor": "#EEB20055"}
                for name, rgba in chosen.items():
                    dialog.controls[name].set_value(rgba)
                dialog.chart_controls["chart_background"].set_value("#102030FF")
                indicator, appearance = dialog.values()
                store.write(indicator, appearance)
                reloaded, loaded_app = SettingsStore(store.path).read()
                for name, rgba in chosen.items():
                    self.assertEqual(getattr(reloaded, name), rgba)
                self.assertEqual(loaded_app.chart_background, "#102030FF")
                chart = CandleChart()
                chart.set_theme("dark")  # Old alias must not change the theme.
                chart.set_settings(reloaded)
                chart.set_chart_colors(loaded_app)
                self.assertEqual(chart.visual_theme, "user")
                for side, broken, expected in ((1, False, "#1246ab"),
                                               (-1, False, "#ed2895"),
                                               (1, True, "#eeb200")):
                    image = QImage(320, 140, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(QColor("#102030"))
                    painter = QPainter(image)
                    chart._draw_zone(painter, 10, 295, 20, 95,
                                     side, broken, "H1", 12)
                    self.assertEqual(painter.pen().color().name(), expected)
                    painter.end()
                chart.close()
            finally:
                dialog.close()

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
        self.assertIsNotNone(dialog.panel)  # Legacy MainWindow still uses it.
        self.assertTrue(dialog.panel.isChecked())
        self.assertFalse(any("Время свечи" in widget.text() for widget in
                             dialog.findChildren(QLabel)))
        dialog.restore_defaults()
        indicator,application=dialog.values()
        self.assertEqual(indicator.as_dict(),IndicatorSettings().as_dict())
        self.assertEqual((application.auto_updates,application.refresh_seconds,
                          application.show_panel,application.allow_http),
                         (True,60,False,False))
        for field,_ in CHART_COLOR_FIELDS:
            self.assertEqual(getattr(application,field),CHART_THEME_DEFAULTS['user'][field])
        dialog.close()
