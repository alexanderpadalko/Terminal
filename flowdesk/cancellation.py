"""Cooperative cancellation for a worker's HTTP pages and retry pauses."""
from contextlib import contextmanager
import threading
import time

_state = threading.local()


class Cancelled(BaseException):
    """A shutdown request, not an HTTP error eligible for cache fallback."""


def checkpoint():
    if getattr(_state, 'cancelled', lambda: False)():
        raise Cancelled()


@contextmanager
def scope(cancelled):
    previous = getattr(_state, 'cancelled', None)
    _state.cancelled = cancelled
    try:
        checkpoint()
        yield
    finally:
        if previous is None:
            del _state.cancelled
        else:
            _state.cancelled = previous


def pause(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        checkpoint()
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
