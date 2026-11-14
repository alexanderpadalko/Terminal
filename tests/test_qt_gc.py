import gc
import threading
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from flowdesk.qt_gc import install_gui_gc


class TestGuiGarbageCollection(unittest.TestCase):
    def test_cycle_collection_runs_on_gui_thread_only(self):
        app=QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        collector=install_gui_gc(app)
        self.assertIs(install_gui_gc(app),collector)
        self.assertFalse(gc.isenabled())
        with patch("flowdesk.qt_gc.gc.collect") as collect:
            worker=threading.Thread(target=collector.collect)
            worker.start();worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            collect.assert_not_called()
            collector.collect()
            collect.assert_called_once()
