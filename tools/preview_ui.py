"""Deterministic visual QA of the real Qt chart, with no market request."""
import base64
import os
import sys
from pathlib import Path

# Windows' offscreen plugin can draw empty glyph boxes. Render the real
# Windows backend into an unmapped widget, using the installed system fonts.
os.environ['QT_QPA_PLATFORM'] = 'windows' if os.name == 'nt' else 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication
from flowdesk.gui import MainWindow

app = QApplication([])
window = MainWindow(startup=False, watchlist_service=False)
window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
window.resize(1440, 900)
window.show()
app.processEvents()
assert QFontMetrics(window.font()).inFontUcs4(ord('П')), 'Russian font glyphs unavailable in preview'
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

