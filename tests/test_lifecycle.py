import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from flowdesk.cancellation import Cancelled, checkpoint, pause, scope
from flowdesk.lifecycle import ShutdownWatchdog


class TestCancellation(unittest.TestCase):
    def test_scope_is_local_and_bypasses_http_fallback(self):
        with self.assertRaises(Cancelled):
            with scope(lambda: True):
                self.fail('cancelled scope entered')
        checkpoint()

    def test_retry_pause_stops_without_waiting_for_full_delay(self):
        stop = threading.Event()
        timer = threading.Timer(.02, stop.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            with scope(stop.is_set):
                pause(10)
        timer.join()
        self.assertLess(time.monotonic() - started, .7)

    def test_watchdog_close_callbacks_cannot_restart_deadline(self):
        exited = threading.Event()
        watchdog = ShutdownWatchdog(.04, exit_process=lambda code: exited.set())
        watchdog.arm()
        first = watchdog.timer
        watchdog.arm()
        self.assertIs(watchdog.timer, first)
        self.assertTrue(exited.wait(1))


@unittest.skipUnless(os.environ.get('CI') == 'true', 'Qt subprocess exits checked on Windows CI')
class TestProcessExit(unittest.TestCase):
    def test_real_process_exits_even_when_worker_or_dns_never_returns(self):
        root = Path(__file__).resolve().parents[1]
        for scenario in ('idle', 'http', 'list', 'stream', 'executor'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run([sys.executable, str(root/'tools/shutdown_probe.py'), scenario, directory],
                    cwd=root, capture_output=True, text=True, timeout=8,
                    env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('QThread: Destroyed', result.stderr)
                info = json.loads((Path(directory)/'close.json').read_text())
                self.assertLess(time.time() - info['closed_at'], 5)
                with sqlite3.connect(Path(directory)/'probe.sqlite3') as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0], 'committed')
