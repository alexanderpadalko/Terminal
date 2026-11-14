"""Collect Python cycles containing Qt wrappers only on the GUI thread.

Automatic CPython GC can run in a calculation worker and finalize unreachable
PySide widgets there. Refcount cleanup remains unchanged; cyclic GC is driven
by the application's main-thread timer instead.
"""
import gc
import threading
from PySide6.QtCore import QObject, QTimer


class GuiGarbageCollector(QObject):
    def __init__(self,app):
        super().__init__(app)
        self.main_thread=threading.get_ident()
        self.was_enabled=gc.isenabled()
        self.ticks=0
        gc.disable()
        self.timer=QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.collect)
        self.timer.start()
        app.aboutToQuit.connect(self.restore)

    def collect(self):
        if threading.get_ident()!=self.main_thread:return
        self.ticks+=1
        gc.collect(2 if self.ticks%10==0 else 0)

    def restore(self):
        self.timer.stop()
        if self.was_enabled:gc.enable()


def install_gui_gc(app):
    collector=getattr(app,"_flowdesk_gc",None)
    if collector is None:
        collector=GuiGarbageCollector(app)
        app._flowdesk_gc=collector
    return collector
