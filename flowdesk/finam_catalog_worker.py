"""Nonblocking read-only Finam directory lookup for curated watchlist."""
from PySide6.QtCore import QThread, Signal


class FinamCatalogWorker(QThread):
    loaded=Signal(object)
    failed=Signal(str)

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api=api

    def run(self):
        try:
            rows=self.api.active_assets(cancelled=self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.loaded.emit(rows)
        except Exception as exc:
            if not self.isInterruptionRequested():
                # Do not leak secret, JWT, or server body.
                self.failed.emit(f"{type(exc).__name__}: {str(exc)[:170]}")
