"""MOEX ISS is used solely for the exact 20-session stock-turnover ranking."""
from .watchlist import WatchlistService
from .finam_universe import cached_top20


def refresh_top20(*,watchlist=None,cancelled=lambda:False):
    service=watchlist or WatchlistService()
    previous=service.cached()
    if previous is None or service.due(previous):
        try:
            service.refresh(cancelled=cancelled,stocks_only=True)
        except InterruptedError:
            raise
        except Exception:
            rows,status=cached_top20(watchlist=service)
            return rows,status+" · MOEX недоступна; сохранён прежний состав"
    rows,status=cached_top20(watchlist=service)
    if len(rows)!=20:
        raise ValueError("MOEX: рейтинга ровно из 20 акций нет")
    return rows,status+" · MOEX: 20 завершённых торговых сессий"
