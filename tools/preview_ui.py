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

# A deliberately delayed synthetic fixture proves that the visible label is
# the candle start 11:23, while receipt 11:38:43 stays in a tooltip only.
from datetime import datetime, timedelta
from flowdesk.gui import demo_data
from flowdesk.engine import analyze
from flowdesk.market_data import Candle, MOSCOW
sample = demo_data()[-120:]
last = datetime(2026, 11, 12, 11, 23, tzinfo=MOSCOW)
bars = [Candle(last-timedelta(minutes=119-i), c.open, c.high, c.low, c.close, c.volume)
        for i, c in enumerate(sample)]
window.data_info = {'tier': 'network', 'asof': '2026-11-12T11:23:43+03:00',
                   'requested_at': '2026-11-12T11:38:43+03:00', 'feed_delay_seconds': 900}
window._show_data(analyze(bars, 'M1'), {}, 'DEMO (синтетическая проверка времени)')
window.chart.instrument_header = 'ПРОВЕРКА ВРЕМЕНИ · СИНТЕТИЧЕСКИЕ OHLCV'
app.processEvents()
chart = window.chart
left, _ = chart.viewport.bounds(len(bars))
chart.cursor_x = chart.x0 + (len(bars)-.5-left)*(chart.x1-chart.x0)/chart.viewport.span
chart.cursor_y = (chart.top+chart.bottom)/2
window._hover(len(bars)-1)
chart.update()
app.processEvents()
assert '11:23 МСК' in window.candle_time.text()
assert '11:38' not in window.data_age.text()
destination = Path('candle-time-preview.png')
window.grab().save(str(destination), 'PNG')
print('CANDLE_TIME_PREVIEW_PNG_BEGIN')
encoded = base64.b64encode(destination.read_bytes()).decode()
for i in range(0, len(encoded), 4096):
    print(encoded[i:i + 4096])
print('CANDLE_TIME_PREVIEW_PNG_END')
window.close()

