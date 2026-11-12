"""Run in a CHILD process only: exercise explicit close against stuck workers."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication
from flowdesk.gui import MainWindow, demo_data
from flowdesk.engine import analyze
from flowdesk.lifecycle import ShutdownWatchdog
from flowdesk.watchlist import WatchlistService


def main():
    scenario, directory = sys.argv[1], Path(sys.argv[2])
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    app = QApplication([])
    gate = threading.Event()
    with sqlite3.connect(directory/'probe.sqlite3') as db:
        db.execute('CREATE TABLE state(value TEXT)')
        db.execute("INSERT INTO state VALUES('committed')")

    class Market:
        def view(self, symbol, board, tf, **kw):
            if scenario == 'http':
                # An interrupted SQLite transaction must roll back on exit.
                with sqlite3.connect(directory/'probe.sqlite3') as db:
                    db.execute("UPDATE state SET value='uncommitted'")
                    gate.wait()
            return analyze(demo_data(), tf), {}, {'symbol': symbol, 'board': board, 'tf': tf, 'tier': 'memory',
                'asof': '2026-11-12T07:00:00+00:00', 'warning': ''}

    class Lists:
        def cached(self): return WatchlistService().cached()
        def due(self, value): return True
        def refresh(self, stop): gate.wait()

    class StuckStream(QThread):
        event = Signal(object)
        def run(self): gate.wait()
        def stop(self): pass

    watchdog = ShutdownWatchdog()
    app.aboutToQuit.connect(watchdog.arm)
    window = MainWindow(service=Market(), startup=scenario=='http',
        watchlist_service=Lists() if scenario=='list' else False, shutdown=watchdog)
    window.show()
    if scenario == 'stream':
        window.stream_worker = StuckStream()
        window.stream_worker.finished.connect(window._stream_finished)
        window.stream_worker.start()
    if scenario == 'executor':
        # Reproduce asyncio.run's wait for a default executor doing DNS/I/O,
        # after the Qt window has already closed.
        threading.Thread(target=lambda: asyncio.run(asyncio.to_thread(gate.wait))).start()
    def close():
        (directory/'close.json').write_text(json.dumps({'closed_at': time.time(), 'scenario': scenario}))
        window.close()
    QTimer.singleShot(150, close)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
