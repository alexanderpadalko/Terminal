"""MOEX ISS is used solely for the exact 20-session stock-turnover ranking."""
from .watchlist import WatchlistService
from .finam_universe import STOCK_DISPLAY_LIMIT, cached_top10
from .diagnostics_log import log_event
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
import time
from .market_data import MOSCOW
from .watchlist import HISTORY_POLICY


def ranking_current(value,now):
    if not value or value.get('history_policy')!=HISTORY_POLICY or len(value.get('window',()))!=20:
        return False
    day=value.get('ranking_day')
    if not day:
        try:day=datetime.fromisoformat(value['generated_at']).astimezone(MOSCOW).date().isoformat()
        except (ValueError,KeyError,TypeError):return False
    return day==now.astimezone(MOSCOW).date().isoformat()


@contextmanager
def ranking_lock(service,cancelled):
    # Serialise concurrent startup windows/processes, then re-read the cache.
    from PySide6.QtCore import QLockFile
    path=getattr(service,'path',None)
    if path is None:
        yield;return
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    lock=QLockFile(str(path)+'.ranking.lock');lock.setStaleLockTime(300000)
    started=time.monotonic()
    while not lock.tryLock(0):
        if cancelled():raise InterruptedError()
        if time.monotonic()-started>120:raise TimeoutError('Stock ranking busy')
        time.sleep(.05)
    try:yield
    finally:lock.unlock()


def refresh_top10(*,watchlist=None,cancelled=lambda:False):
    service=watchlist or WatchlistService()
    with ranking_lock(service,cancelled):
        return _refresh_top10(service,cancelled)


def _refresh_top10(service,cancelled):
    previous=service.cached()
    now=service.clock() if callable(getattr(service,'clock',None)) else datetime.now(MOSCOW)
    if not ranking_current(previous,now):
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
