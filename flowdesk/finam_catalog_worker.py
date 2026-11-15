"""Nonblocking startup: Finam catalog + MOEX top-20 + date-based roll.

The next expiry is selected on the calendar day before confirmed expiration.
No turnover/volume API calls for the roll decision. MOEX ISS is used solely
for stock turnover ranking. All expiry and market data come from Finam.
"""
from datetime import datetime, timezone
from contextlib import nullcontext

from PySide6.QtCore import QThread, Signal

from .finam_universe import (
    cached_top20, US_CONTRACTS, RU_CONTRACTS,
)
from .finam_rotation import RollState, resolve_futures
from .diagnostics_log import log_event, log_contract
from .moex_reference import refresh_top20
from .watchlist import WatchlistService
from .finam_futures_cache import FuturesListCache


class FinamCatalogWorker(QThread):
    futures_ready=Signal(object)
    loaded=Signal(object)
    failed=Signal(str)
    progress=Signal(str)

    def __init__(self,api,parent=None,*,watchlist=None,roll_state=None,clock=None,
                 futures_cache=None):
        super().__init__(parent)
        self.api=api
        self.watchlist=watchlist or WatchlistService()
        self.roll_state=roll_state or RollState()
        self.futures_cache=futures_cache or FuturesListCache()
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def run(self):
        scope=getattr(self.api,"request_scope",None)
        with scope(priority=5,cancelled=self.isInterruptionRequested) if scope else nullcontext():
            self._run()

    def _run(self):
        try:
            log_event("catalog_start")
            self.progress.emit("Finam: проверка действующих контрактов и списка акций…")
            finam_error=""
            try:
                assets=self.api.active_assets(cancelled=self.isInterruptionRequested)
            except InterruptedError:
                raise
            except Exception as exc:
                log_event("catalog_unavailable",error=type(exc).__name__)
                assets=[]
                finam_error="Finam: каталог недоступен; фьючерсы не переключаются"
            if self.isInterruptionRequested():
                return
            # Resolve futures before downloading the independent MOEX stock
            # ranking: slow MOEX must not hold the US/RU contract checks.
            previous=self.roll_state.load()
            # GetAsset may have cached a stale no-expiry response; recheck
            # must hit Finam again without exposing credentials.
            clear=getattr(self.api,"clear_asset_details_cache",None)
            if callable(clear):
                clear()
            futures,selections,notes=resolve_futures(
                assets,self.api,now=self.clock(),previous=previous,
                cancelled=self.isInterruptionRequested,
                progress=self.progress.emit)
            if self.isInterruptionRequested():
                return
            # Only allow-listed metadata is recorded: no raw GetAsset,
            # exception messages, credentials, or account identifiers.
            for c in (*US_CONTRACTS,*RU_CONTRACTS):
                row=futures.get(c.group+":"+c.display)
                if row is not None:
                    log_contract(c,row)
            log_event("catalog_complete")
            # A transient catalog/metadata outage must not forget an earlier
            # confirmed rollover or silently revert to the original series.
            self.roll_state.save({**previous,**selections})
            # Each market receives its own atomic daily snapshot. A failed
            # metadata check cannot overwrite a previously verified entry.
            try:
                self.futures_cache.save(futures,now=self.clock())
            except OSError:
                log_event("futures_snapshot_unavailable",error="OSError")
            if self.isInterruptionRequested():
                return
            # Let US/RU lists update independently of a potentially slow
            # MOEX top-20 query, without enabling unverified symbols.
            self.futures_ready.emit(futures)
            self.progress.emit("MOEX: обновление топ-20 по обороту за 20 сессий…")
            try:
                stocks,ranking_status=refresh_top20(
                    watchlist=self.watchlist,cancelled=self.isInterruptionRequested)
            except InterruptedError:
                raise
            except Exception as exc:
                log_event("stock_ranking_unavailable",error=type(exc).__name__)
                stocks,ranking_status=cached_top20(watchlist=self.watchlist)
                ranking_status+=" · ошибка MOEX, сохранён прошлый рейтинг"
            if self.isInterruptionRequested():
                return
            # Remap any saved ticker from the prior active tenor to the new
            # one. Also remap initial screenshot aliases (e.g. NQZ6@XCME).
            roll_map={}
            for c in (*US_CONTRACTS,*RU_CONTRACTS):
                key=c.group+":"+c.display
                row=futures.get(key)
                if row is None or not row.verified:
                    continue
                for mic in c.mics:
                    for alias in c.aliases:
                        roll_map[alias.upper()+"@"+mic]=row.symbol
                prior=previous.get(key)
                if prior:
                    roll_map[prior.upper()]=row.symbol
            self.loaded.emit({
                "assets":assets,
                "finam_error":finam_error,
                "stocks":stocks,
                "ranking_status":ranking_status,
                "futures":futures,
                "roll_map":roll_map,
                "roll_notes":notes,
            })
        except InterruptedError:
            return
        except Exception as exc:
            if not self.isInterruptionRequested():
                # Only exception types are logged/displayed, never raw messages:
                # downstream code may interpolate sensitive request details.
                log_event("catalog_worker_failed",error=type(exc).__name__)
                self.failed.emit(type(exc).__name__)
