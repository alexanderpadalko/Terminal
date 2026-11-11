"""Deterministic visual QA of the real Qt chart, with no market request."""
import base64
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
from flowdesk.gui import MainWindow

app = QApplication([])
window = MainWindow()
window.resize(1440, 900)
window.show()
app.processEvents()
window.chart.cursor_x = (window.chart.x0 + window.chart.x1) / 2
window.chart.cursor_y = (window.chart.top + window.chart.bottom) / 2
window.chart.update()
app.processEvents()
destination = Path('ui-preview.png')
window.grab().save(str(destination), 'PNG')
print('UI_PREVIEW_PNG_BEGIN')
encoded = base64.b64encode(destination.read_bytes()).decode()
for i in range(0, len(encoded), 4096):
    print(encoded[i:i + 4096])
print('UI_PREVIEW_PNG_END')
window.close()
