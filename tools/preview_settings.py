"""Render the actual gear dialog and chart, verify persisted UI input coverage."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
os.environ['QT_QPA_PLATFORM']='windows' if os.name=='nt' else 'offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from flowdesk.gui import MainWindow
from flowdesk.settings import SettingsStore
from flowdesk.settings_dialog import SettingsDialog

app=QApplication.instance() or QApplication([])
app.setQuitOnLastWindowClosed(False)
with tempfile.TemporaryDirectory() as directory:
    store=SettingsStore(Path(directory)/'settings.json')
    window=MainWindow(startup=False,watchlist_service=False,settings_store=store)
    window.resize(1500,900)
    window.show()
    app.processEvents()
    window.grab().save('settings-chart-preview.png')
    dialog=SettingsDialog(window.indicator_settings,window.application_settings,window)
    dialog.applied.connect(window._apply_settings)
    dialog.show()
    dialog.tabs.setCurrentIndex(3)
    app.processEvents()
    dialog.grab().save('settings-dialog-preview.png')
    assert len(dialog.controls)==74
    dialog.controls['fvgMaxZones'].setValue(9)
    dialog.controls['flowShowData'].setChecked(True)
    dialog.apply()
    restored,_=SettingsStore(store.path).read()
    assert restored.fvgMaxZones==9 and restored.flowShowData
    assert window.chart.fvg_settings.max_zones==9
    result={'inputs':len(dialog.controls),'tabs':dialog.tabs.count(),'restored_limit':restored.fvgMaxZones,
            'restored_flow_labels':restored.flowShowData,'reference':window.analysis.reference_version}
    Path('settings-ui-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
    for name in ('settings-chart-preview.png','settings-dialog-preview.png'):
        print('SETTINGS_PREVIEW_BEGIN '+name)
        print(base64.b64encode(Path(name).read_bytes()).decode())
        print('SETTINGS_PREVIEW_END')
    dialog.close()
    window.close()
