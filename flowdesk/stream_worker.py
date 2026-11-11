"""Qt lifecycle wrapper; all network I/O lives off the GUI thread."""
import asyncio
import threading

from PySide6.QtCore import QThread, Signal

from .iss_plus import IssPlusClient


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
            if self._stop.is_set():
                return
            try:
                await IssPlusClient(self.symbol, self.board).run(self.event.emit)
            except asyncio.CancelledError:
                pass
            finally:
                self._task = self._loop = None
        asyncio.run(main())
