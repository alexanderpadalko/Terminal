"""Route public crypto separately from Finam; never combine candle stores."""
from pathlib import Path
import threading
from .finam_market import FinamMarketService
from .binance_api import BinanceAPI
from .market_symbols import BINANCE


class MarketServices:
    def __init__(self,*,finam=None,crypto=None,directory=None):
        self.finam=finam or FinamMarketService()
        self.api=self.finam.api  # Finam catalog/auth stay with Finam only.
        self.crypto=crypto
        self.directory=Path(directory or Path.home()/".flowdesk")
        self._lock=threading.Lock()

    def for_market(self,venue):
        if venue!=BINANCE:return self.finam
        with self._lock:
            if self.crypto is None:
                self.crypto=FinamMarketService(self.directory/"binance_candles_v1.sqlite3",api=BinanceAPI())
            return self.crypto

    def for_symbol(self,symbol):return self.for_market(symbol.rsplit("@",1)[1])
    def mark_stream_reconnect(self,symbol):self.for_symbol(symbol).mark_stream_reconnect(symbol)
    def ingest_quote(self,event):self.for_market(event.board).ingest_quote(event)
    def ingest_stream(self,ticker,venue,events):self.for_market(venue).ingest_stream(ticker,venue,events)
    def invalidate_network_cache(self):self.finam.invalidate_network_cache()
