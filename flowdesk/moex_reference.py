"""MOEX ISS is used solely for the exact 20-session stock-turnover ranking."""
from .watchlist import WatchlistService
from .finam_universe import STOCK_DISPLAY_LIMIT, cached_top10
from .diagnostics_log import log_event


def refresh_top10(*,watchlist=None,cancelled=lambda:False):
    service=watchlist or WatchlistService()
    previous=service.cached()
    if previous is None or service.due(previous):
        try:
            service.refresh(cancelled=cancelled,stocks_only=True)
        except InterruptedError:
            raise
        except Exception as exc:
            log_event("stock_ranking_unavailable",error=exc,stage="stock_ranking")
            rows,status=cached_top10(watchlist=service)
            return rows,status+" · MOEX недоступна; сохранён прежний состав"
    rows,status=cached_top10(watchlist=service)
    if len(rows)!=STOCK_DISPLAY_LIMIT:
        raise ValueError("MOEX: рейтинга ровно из 10 акций нет")
    return rows,status+" · MOEX: 20 завершённых торговых сессий"
