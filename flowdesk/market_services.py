"""Route public crypto separately from Finam; never combine candle stores."""
from pathlib import Path
import threading
from .finam_market import FinamMarketService
from .market_symbols import BINANCE
from .storage_safety import backup_if_due
from .diagnostics_log import log_event


class MarketServices:
    def __init__(self,*,finam=None,crypto=None,directory=None,legacy_crypto=False):
        self.finam=finam or FinamMarketService()
        self.api=self.finam.api  # Finam catalog/auth stay with Finam only.
        self.crypto=crypto
        self.legacy_crypto=legacy_crypto
        self.directory=Path(directory or Path.home()/".flowdesk")
        self._lock=threading.Lock()
        self._backup_lock=threading.Lock()
        self._backup_active=False

    def backup_if_due_async(self):
        """Take daily verified SQLite snapshots in a daemon background worker."""
        with self._backup_lock:
            if self._backup_active:
                return False
            self._backup_active=True

        def worker():
            try:
                # Crypto is lazy: do not create a new Binance database
                # merely to take a backup.
                for service in (self.finam,self.crypto):
                    if service is None:
                        continue
                    try:
                        backup_if_due(service.path)
                    except Exception as exc:
                        # Only safe type/category/status, never paths or messages.
                        log_event("storage_snapshot_unavailable",
                                  error=exc,stage="backup")
            finally:
                with self._backup_lock:
                    self._backup_active=False

        threading.Thread(target=worker,daemon=True,
                         name="FlowDesk-storage-backup").start()
        return True

    def for_market(self,venue):
        if venue!=BINANCE:return self.finam
        if not self.legacy_crypto:raise ValueError('Криптовалюты исключены из терминала')
        with self._lock:
            if self.crypto is None:
                from .binance_api import BinanceAPI
                self.crypto=FinamMarketService(self.directory/"binance_candles_v1.sqlite3",api=BinanceAPI())
            return self.crypto

    def for_symbol(self,symbol):return self.for_market(symbol.rsplit("@",1)[1])
    def mark_stream_reconnect(self,symbol):self.for_symbol(symbol).mark_stream_reconnect(symbol)
    def ingest_quote(self,event):self.for_market(event.board).ingest_quote(event)
    def ingest_stream(self,ticker,venue,events):self.for_market(venue).ingest_stream(ticker,venue,events)
    def invalidate_network_cache(self):self.finam.invalidate_network_cache()
