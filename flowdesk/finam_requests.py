"""Shared, cancellable Finam HTTP scheduling. No credentials are logged."""
from collections import deque
from concurrent.futures._base import Future
from contextlib import contextmanager
import heapq
import itertools
import threading
import time


class RequestCoordinator:
    def __init__(self, *, per_minute=180, concurrency=3, clock=time.monotonic):
        if per_minute<=0 or concurrency<1:raise ValueError("Invalid Finam request budget")
        self.per_minute = per_minute
        self.concurrency = concurrency
        self.clock = clock
        self.condition = threading.Condition()
        self.waiting = []
        self.sequence = itertools.count()
        self.started = deque()
        self.next_start = 0.0
        self.blocked_until = 0.0
        self.active = 0
        self.inflight = {}
        self.local = threading.local()

    @contextmanager
    def scope(self, *, priority=10, cancelled=lambda: False):
        previous = getattr(self.local, "scope", None)
        self.local.scope = (priority, cancelled)
        try:
            yield
        finally:
            self.local.scope = previous

    def _acquire(self, priority, cancelled):
        ticket = (priority, next(self.sequence))
        with self.condition:
            heapq.heappush(self.waiting, ticket)
            try:
                while True:
                    if cancelled():
                        raise InterruptedError()
                    now = self.clock()
                    while self.started and self.started[0] <= now - 60:
                        self.started.popleft()
                    due = max(self.next_start, self.blocked_until)
                    if len(self.started) >= self.per_minute:
                        due = max(due, self.started[0] + 60)
                    if (self.waiting[0] == ticket and self.active < self.concurrency
                            and now >= due):
                        heapq.heappop(self.waiting)
                        self.active += 1
                        self.started.append(now)
                        self.next_start = now + 60 / self.per_minute
                        return
                    self.condition.wait(min(.1, due-now) if due>now else .1)
            finally:
                if ticket in self.waiting:
                    self.waiting.remove(ticket)
                    heapq.heapify(self.waiting)
                self.condition.notify_all()

    def run(self, key, send):
        priority, cancelled = getattr(self.local, "scope", None) or (5, lambda: False)
        if cancelled():
            raise InterruptedError()
        with self.condition:
            future = self.inflight.get(key) if key is not None else None
            owner = future is None
            if owner:
                future = Future()
                if key is not None:
                    self.inflight[key] = future
        if not owner:
            while True:
                if cancelled():
                    raise InterruptedError()
                try:
                    return future.result(timeout=.1)
                except TimeoutError:
                    if future.done():raise
        try:
            for attempt in range(2):
                self._acquire(priority, cancelled)
                try:
                    result = send()
                except Exception as exc:
                    if getattr(exc, "http_status", None) != 429:
                        raise
                    delay = getattr(exc, "retry_after", None) or 60
                    with self.condition:
                        self.blocked_until = max(self.blocked_until, self.clock() + delay)
                    if attempt:
                        raise
                else:
                    future.set_result(result)
                    return result
                finally:
                    with self.condition:
                        self.active -= 1
                        self.condition.notify_all()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.condition:
                if key is not None:
                    self.inflight.pop(key, None)
                self.condition.notify_all()
