"""Qt lifecycle wrapper; all network I/O lives off the GUI thread."""
import asyncio
import threading
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

from .iss_plus import FeedEvent, IssPlusClient
from .runtime_errors import record_exception


class StreamWorker(QThread):
    event = Signal(object)

    def __init__(self, symbol, board, parent=None):
        super().__init__(parent)
        self.symbol, self.board = symbol, board
        self._stop = threading.Event()
        self._loop = self._task = None

    def stop(self):
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def run(self):
        async def main():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            try:
                if not self._stop.is_set():
                    await IssPlusClient(self.symbol, self.board).run(self.event.emit)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                record_exception('stream_worker', exc)
                self.event.emit(FeedEvent('status', self.symbol, self.board, 1, datetime.now(timezone.utc),
                                          state='unavailable', detail=f'{type(exc).__name__}: {exc}'))
            finally:
                self._task = self._loop = None
        asyncio.run(main())
