"""Nonblocking startup Finam catalog + MOEX top-20 + liquidity-based rolls.

Finam supplies the instruments, expiry and the US-futures quote metrics.
MOEX ISS supplies ONLY reference turnover/volume for Russian instruments
and the last 20 completed share trading sessions. No orders, no MOEX candles.
"""
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

from .finam_universe import (
    cached_top20, US_CONTRACTS, RU_CONTRACTS,
)
from .finam_rotation import RollState, resolve_futures
from .moex_reference import refresh_top20, MoexFuturesVolume
from .watchlist import WatchlistService


class FinamCatalogWorker(QThread):
    loaded=Signal(object)
    failed=Signal(str)
    progress=Signal(str)

    def __init__(self,api,parent=None,*,watchlist=None,roll_state=None,
                 moex_volume=None,clock=None):
        super().__init__(parent)
        self.api=api
        self.watchlist=watchlist or WatchlistService()
        self.roll_state=roll_state or RollState()
        self.moex_volume=moex_volume or MoexFuturesVolume()
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
            self.progress.emit("MOEX: сверка объёмов российских фьючерсов…")
            moex=None
            moex_status="MOEX RFUD: обороты недоступны, переход не подтверждён"
            try:
                self.moex_volume.refresh(cancelled=self.isInterruptionRequested)
                moex=self.moex_volume
                moex_status="MOEX RFUD: обороты и объёмы проверены"
            except InterruptedError:
                raise
            except Exception:
                pass
            if self.isInterruptionRequested():
                return
            previous=self.roll_state.load()
            futures,selections,notes=resolve_futures(
                assets,self.api,now=self.clock(),previous=previous,
                moex_volume=moex,cancelled=self.isInterruptionRequested,
                progress=self.progress.emit)
            if self.isInterruptionRequested():
                return
            self.roll_state.save(selections)
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
