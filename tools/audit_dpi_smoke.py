"""Headless simulated-DPI checks for the analytical chart settings layout.

These are virtual Qt scaling profiles, not a substitute for checking
physical high-DPI Windows monitors and GPU drivers.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")


def main():
    from PySide6.QtWidgets import QApplication
    from flowdesk.settings_dialog import SettingsDialog, ColorPalettePopup
    from flowdesk.settings import IndicatorSettings, ApplicationSettings
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory(prefix="flowdesk-dpi-") as temp:
        with patch.object(Path,"home",return_value=Path(temp)):
            dialog=SettingsDialog(IndicatorSettings(),ApplicationSettings(),
                                  finam_only=True)
            try:
                dialog.resize(960,700)
                dialog.show()
                app.processEvents()
                colors=dialog.controls
                checks=[]
                for key in ("fvgBullColor","fvgBearColor","fvgFilledColor"):
                    color=colors[key]
                    popup=ColorPalettePopup(color)
                    try:
                        popup.show()
                        app.processEvents()
                        if popup.percent.width()<90:
                            raise AssertionError("Clipped opacity control: "+key)
                        if len(popup.swatches)!=80:
                            raise AssertionError("Incomplete color swatches")
                        if not popup.isVisible():
                            raise AssertionError("Palette did not open")
                        checks.append({"field":key,
                                       "spin_width":popup.percent.width(),
                                       "swatches":len(popup.swatches)})
                    finally:
                        popup.close()
                screen=app.primaryScreen()
                report={"status":"PASS","qt_scale_factor":os.environ.get("QT_SCALE_FACTOR","1"),
                        "device_pixel_ratio":screen.devicePixelRatio(),
                        "color_editors":checks}
                print("FLOWDESK_DPI_SMOKE_OK "+json.dumps(report))
            finally:
                dialog.close()
                app.processEvents()


if __name__=="__main__":
    main()
