"""Nonblocking startup: Finam catalog + MOEX top-10 + date-based roll.

The next expiry is selected on the calendar day before confirmed expiration.
No turnover/volume API calls for the roll decision. MOEX ISS is used solely
for stock turnover ranking. All expiry and market data come from Finam.
"""
from datetime import datetime, timezone
from contextlib import nullcontext
from time import monotonic

from PySide6.QtCore import QThread, Signal

from .finam_universe import (
    cached_top10, US_CONTRACTS, RU_CONTRACTS,
)
from .finam_rotation import RollState, resolve_futures
from .diagnostics_log import log_event, log_contract
from .moex_reference import refresh_top10
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
        started=phase_started=monotonic()
        stage="catalog"
        try:
            log_event("catalog_start",stage="catalog_refresh")
            self.progress.emit("Finam: проверка действующих контрактов и списка акций…")
            finam_error=""
            try:
                assets=self.api.active_assets(cancelled=self.isInterruptionRequested)
            except InterruptedError:
                raise
            except Exception as exc:
                log_event("catalog_unavailable",error=exc,stage=stage,
                          elapsed_ms=(monotonic()-phase_started)*1000)
                assets=[]
                finam_error="Finam: каталог недоступен; фьючерсы не переключаются"
            if self.isInterruptionRequested():
                return
            # Resolve futures before downloading the independent MOEX stock
            # ranking: slow MOEX must not hold the US/RU contract checks.
            stage="roll_state_load"
            previous=self.roll_state.load()
            # GetAsset may have cached a stale no-expiry response; recheck
            # must hit Finam again without exposing credentials.
            stage="futures_resolution"
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
            # A transient catalog/metadata outage must not forget an earlier
            # confirmed rollover or silently revert to the original series.
            stage="roll_state_save"
            self.roll_state.save({**previous,**selections})
            # Each market receives its own atomic daily snapshot. A failed
            # metadata check cannot overwrite a previously verified entry.
            stage="futures_snapshot"
            snapshot_failed=False
            try:
                self.futures_cache.save(futures,now=self.clock())
            except OSError as exc:
                snapshot_failed=True
                log_event("futures_snapshot_unavailable",error=exc,stage=stage)
            if self.isInterruptionRequested():
                return
            # Let US/RU lists update independently of a potentially slow
            # MOEX top-10 query (20 completed sessions), without enabling unverified symbols.
            self.futures_ready.emit(futures)
            stage="stock_ranking"
            phase_started=monotonic()
            self.progress.emit("MOEX: обновление топ-10 по обороту за 20 сессий…")
            try:
                stocks,ranking_status=refresh_top10(
                    watchlist=self.watchlist,cancelled=self.isInterruptionRequested)
            except InterruptedError:
                raise
            except Exception as exc:
                log_event("stock_ranking_unavailable",error=exc,stage=stage,
                          elapsed_ms=(monotonic()-phase_started)*1000)
                stocks,ranking_status=cached_top10(watchlist=self.watchlist)
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
            stage="catalog_publish"
            self.loaded.emit({
                "assets":assets,
                "finam_error":finam_error,
                "stocks":stocks,
                "ranking_status":ranking_status,
                "futures":futures,
                "roll_map":roll_map,
                "roll_notes":notes,
            })
            outcome=("fallback" if finam_error else "partial" if snapshot_failed or
                not FuturesListCache.complete(futures,now=self.clock()) or
                "MOEX недоступна" in ranking_status or "ошибка MOEX" in ranking_status else "ok")
            log_event("catalog_complete",stage="catalog_refresh",outcome=outcome,
                      elapsed_ms=(monotonic()-started)*1000)
        except InterruptedError:
            log_event("catalog_complete",stage=stage,outcome="cancelled",
                      elapsed_ms=(monotonic()-started)*1000)
            return
        except Exception as exc:
            if not self.isInterruptionRequested():
                # Only exception types are logged/displayed, never raw messages:
                # downstream code may interpolate sensitive request details.
                log_event("catalog_worker_failed",error=exc,stage=stage,
                          elapsed_ms=(monotonic()-started)*1000)
                self.failed.emit(type(exc).__name__)
