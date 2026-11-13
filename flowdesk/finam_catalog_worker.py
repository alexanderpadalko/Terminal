"""Nonblocking startup: Finam catalog + MOEX top-20 + date-based roll.

The next expiry is selected on the calendar day before confirmed expiration.
No turnover/volume API calls for the roll decision. MOEX ISS is read-only:
stock turnover ranking, optionally Russian last trading dates. Graph market
data is Finam only; no trades/orders are submitted.
"""
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

from .finam_universe import (
    cached_top20, US_CONTRACTS, RU_CONTRACTS,
)
from .finam_rotation import RollState, resolve_futures
from .exchange_expiry import OfficialExpiry
from .moex_reference import refresh_top20, MoexFuturesDates
from .watchlist import WatchlistService


class FinamCatalogWorker(QThread):
    loaded=Signal(object)
    failed=Signal(str)
    progress=Signal(str)

    def __init__(self,api,parent=None,*,watchlist=None,roll_state=None,
                 moex_dates=None,clock=None,official_expiry=None):
        super().__init__(parent)
        self.api=api
        self.watchlist=watchlist or WatchlistService()
        self.roll_state=roll_state or RollState()
        self.moex_dates=moex_dates or MoexFuturesDates()
        self.official_expiry=official_expiry or OfficialExpiry()
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def run(self):
        try:
            self.progress.emit("Finam: проверка действующих контрактов и списка акций…")
            finam_error=""
            try:
                assets=self.api.active_assets(cancelled=self.isInterruptionRequested)
            except InterruptedError:
                raise
            except Exception:
                assets=[]
                finam_error="Finam: каталог недоступен; фьючерсы не переключаются"
            if self.isInterruptionRequested():
                return
            self.progress.emit("MOEX: обновление топ-20 по обороту за 20 сессий…")
            try:
                stocks,ranking_status=refresh_top20(
                    watchlist=self.watchlist,cancelled=self.isInterruptionRequested)
            except InterruptedError:
                raise
            except Exception:
                stocks,ranking_status=cached_top20(watchlist=self.watchlist)
                ranking_status+=" · ошибка MOEX, сохранён прошлый рейтинг"
            if self.isInterruptionRequested():
                return
            self.progress.emit("MOEX: проверка дат окончания торгов фьючерсов РФ…")
            expiry_overrides={}
            moex_status="MOEX RFUD: даты недоступны; используем Finam"
            try:
                self.moex_dates.refresh(cancelled=self.isInterruptionRequested)
                expiry_overrides=self.moex_dates.dates
                moex_status="MOEX RFUD: даты окончания торгов проверены"
            except InterruptedError:
                raise
            except Exception:
                pass
            if self.isInterruptionRequested():
                return
            previous=self.roll_state.load()
            futures,selections,notes=resolve_futures(
                assets,self.api,now=self.clock(),previous=previous,
                expiry_overrides=expiry_overrides,official=self.official_expiry,
                cancelled=self.isInterruptionRequested,
                progress=self.progress.emit)
            if self.isInterruptionRequested():
                return
            # A transient catalog/metadata outage must not forget an earlier
            # confirmed rollover or silently revert to the original series.
            self.roll_state.save({**previous,**selections})
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
                "moex_status":moex_status,
            })
        except InterruptedError:
            return
        except Exception as exc:
            if not self.isInterruptionRequested():
                # Do not leak credentials, JWTs, request URLs or response body.
                self.failed.emit(f"{type(exc).__name__}: {str(exc)[:170]}")
