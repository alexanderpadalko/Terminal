"""Bound process shutdown even if the OS leaves a socket/DNS call blocked."""
import os
import threading


class ShutdownWatchdog:
    def __init__(self, timeout=2.0, *, exit_process=os._exit):
        self.timeout = timeout
        self.exit_process = exit_process
        self.timer = None
        self._lock = threading.Lock()

    def arm(self):
        # Armed only by an explicit application close/quit, never by a feed
        # failure. Do not restart the deadline as individual workers finish.
        with self._lock:
            if self.timer is not None:
                return
            self.timer = threading.Timer(self.timeout, self.exit_process, args=(0,))
            self.timer.daemon = True
            self.timer.start()

    def cancel(self):
        # Tests may cancel an injected callback. Production keeps the daemon
        # watchdog armed through interpreter exit: a DNS executor can outlive Qt.
        with self._lock:
            if self.timer is not None:
                self.timer.cancel()
