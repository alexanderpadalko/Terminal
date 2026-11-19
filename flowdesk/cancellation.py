"""Cooperative cancellation for work performed on the current thread."""
from contextlib import contextmanager
import threading
import time

_state = threading.local()


class Cancelled(BaseException):
    """A worker interruption that bypasses recoverable data errors."""


def is_cancelled():
    cancelled = getattr(_state, 'cancelled', None)
    return cancelled is not None and cancelled()


def checkpoint():
    if is_cancelled():
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
