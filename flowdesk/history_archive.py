"""Resumable, low-priority full archives, independent of signal readiness.

Request sizes are not retention limits. Walk every source interval, including
empty windows, without stitching symbols or reconstructing missing prices.
1970 is the search floor for the supported instruments, not a claim that the
provider has data that far back. Completion means the range was queried.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from contextlib import closing
import concurrent.futures
import threading
import time

from PySide6.QtCore import QThread, Signal

from .finam_api import DEPTH_DAYS
from .asyncio_runtime import prepare_asyncio_runtime
from .session_store import record_response

VERSION = 'FULL_ARCHIVE_v2_'
FLOOR = datetime(1970, 1, 1, tzinfo=timezone.utc)
FRAMES = ('MN1', 'W1', 'D1', 'H1', 'H4', 'M5', 'M1')


def backfill_window(service, symbol, *, cancelled=lambda: False, frame=None, excluded_frames=()):
    """One durable window; round-robin all seven TFs, including empty ones.

    A new version deliberately starts at now, rather than MIN(saved time):
    old caches may contain holes. Replaying a window is idempotent. Old
    exhausted cursors from the former 30-day/one-year policy cannot stop it.
    No database lock is held during HTTP or rate-limit waits.
    """
    symbol=symbol.upper()
    lock = service._key_lock(service._history_locks, symbol)
    while not lock.acquire(timeout=.1):
        if cancelled():
            raise InterruptedError()
    try:
        if cancelled():
            raise InterruptedError()
        now = service.clock()
        generation = service._generation
        state = service.load_history_progress()
        frames = (frame,) if frame else FRAMES
        if any(tf not in FRAMES for tf in frames):
            raise ValueError('Unknown archive timeframe')
        pending = [(tf, state.get((symbol, VERSION+tf), {})) for tf in frames]
        pending = [(tf, item) for tf, item in pending
                   if not item.get('exhausted') and tf not in excluded_frames]
        if not pending:
            return None
        # Fewest completed windows first keeps M1/M5 moving alongside
        # the larger native windows; persisted turn counts survive restart.
        with closing(service._connection()) as db:
            turns = dict(db.execute('SELECT tf,turns FROM full_archive_turns WHERE symbol=?', (symbol,)))
        tf, item = min(pending, key=lambda row: (turns.get(row[0], 0), FRAMES.index(row[0])))
        end = min(now, item.get('cursor', now))
        scope = getattr(service.api, 'request_scope', None)
        with scope(priority=30, cancelled=cancelled) if scope else nullcontext():
            boundary=getattr(service.api,'archive_start',None)
            earliest=boundary(symbol,tf) if callable(boundary) else None
        if cancelled() or generation!=service._generation:
            raise InterruptedError()
        floor=max(FLOOR,earliest) if earliest is not None else FLOOR
        if floor>now:
            raise ValueError('Invalid archive boundary')
        start = max(floor, end-timedelta(days=DEPTH_DAYS[tf]))
        if end <= floor:
            service.save_history_progress(symbol, VERSION+tf, {'cursor': floor, 'exhausted': True})
            return {'symbol': symbol, 'tf': tf, 'changed': False, 'pending': True}
        with scope(priority=30, cancelled=cancelled) if scope else nullcontext():
            bars = service.api.bars(symbol, tf, start=start, end=end)
        if cancelled():
            raise InterruptedError()
        # The real clients already filter bounds. Also defend this boundary
        # for alternative client implementations: never skip an interval on
        # a response for a different range.
        if any(not start <= bar.time < end for bar in bars):
            raise ValueError('Archive response outside requested interval')
        with service.lock:
            if generation != service._generation:
                raise InterruptedError()
            changed = bool(service._upsert(symbol, tf, bars)) if bars else False
        if getattr(service.api, 'session_candles', False):
            observed = now-timedelta(minutes=service.api.market_delay_minutes(symbol.rsplit('@', 1)[1]))
            record_response(service, symbol, tf, bars, start, end, observed)
        # If interrupted/crashed before this transaction, the next launch
        # repeats the window. Raw bars are never lost or marked complete early.
        with service.lock, closing(service._connection()) as db, db:
            if generation != service._generation:
                raise InterruptedError()
            db.execute('INSERT OR REPLACE INTO finam_history_progress VALUES(?,?,?,?)',
                       (symbol, VERSION+tf, start.isoformat(), int(start == floor)))
            db.execute('INSERT INTO full_archive_turns VALUES(?,?,1) '
                       'ON CONFLICT(symbol,tf) DO UPDATE SET turns=turns+1', (symbol, tf))
        return {'symbol': symbol, 'tf': tf, 'changed': changed or bool(bars), 'pending': True}
    except InterruptedError:
        raise
    except Exception as exc:
        # Fixed internal TF only, no source text. The worker can postpone
        # this frame without starving the other six after a 403/temporary error.
        if 'tf' in locals() and tf in FRAMES:
            exc.archive_timeframe=tf
        raise
    finally:
        lock.release()


class HistoryArchiveWorker(QThread):
    """All selected instruments; price refresh and charts retain priority."""
    changed = Signal(str)

    def __init__(self, services, instruments, parent=None, *, max_workers=1, pace=True):
        super().__init__(parent)
        self.services = services
        self.symbols = tuple(dict.fromkeys(item.symbol.upper() for item in instruments
                            if item.verified and '@' in item.symbol))
        self.max_workers = max_workers
        self.pace=pace

    def run(self):
        prepare_asyncio_runtime()
        stop = threading.Event()
        blocked={symbol:set() for symbol in self.symbols}
        cancelled = lambda: self.isInterruptionRequested() or stop.is_set()
        def one(symbol):
            started=time.thread_time()
            service = (self.services.for_market(symbol.rsplit('@', 1)[1])
                       if callable(getattr(self.services, 'for_market', None)) else self.services)
            result=backfill_window(service, symbol, cancelled=cancelled,
                                   excluded_frames=blocked[symbol])
            if result is not None and self.pace:
                # Archive CPU gets at most roughly one fifth of one core.
                # Network wait does not increase the CPU budget. The request
                # and SQLite locks have already been released before yielding.
                deadline=time.monotonic()+max(.5,4*(time.thread_time()-started))
                while not cancelled() and time.monotonic()<deadline:
                    stop.wait(min(.1,max(0,deadline-time.monotonic())))
            return result
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers,
                                                      thread_name_prefix='FlowDeskArchive')
        active = list(self.symbols)
        try:
            while active and not cancelled():
                again = []
                for begin in range(0, len(active), self.max_workers):
                    if cancelled():
                        break
                    futures = {pool.submit(one, s): s for s in active[begin:begin+self.max_workers]}
                    for future in concurrent.futures.as_completed(futures):
                        symbol = futures[future]
                        try:
                            result = future.result()
                        except InterruptedError:
                            continue
                        except Exception as exc:
                            # Retry failures on the next scheduled worker,
                            # never hammer a failed source or advance its cursor.
                            from .diagnostics_log import log_event
                            log_event('candle_source_unavailable', symbol=symbol,
                                      error=exc, stage='candles', outcome='failed')
                            if getattr(exc, 'http_status', None) == 429:
                                stop.set()
                            elif getattr(exc,'archive_timeframe',None) in FRAMES:
                                blocked[symbol].add(exc.archive_timeframe)
                                again.append(symbol)
                            continue
                        if result is not None:
                            again.append(symbol)
                            if result['changed']:
                                self.changed.emit(symbol)
                active = again
                # Interruptible fair yield between sweeps; no new UI controls.
                stop.wait(.1)
        finally:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
