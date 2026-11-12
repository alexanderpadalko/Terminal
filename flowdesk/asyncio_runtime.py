"""Resolve Python 3.15's lazy executor export before frozen asyncio DNS.

In the affected Windows package, concurrent.futures.ThreadPoolExecutor remains
a non-callable lazy_import. Importing the concrete submodule is visible to
PyInstaller and binding the public export also preserves asyncio's type check.
No event-loop policies, socket addresses or TLS validation are changed.
"""
import concurrent.futures
from concurrent.futures.thread import ThreadPoolExecutor


def prepare_asyncio_runtime():
    concurrent.futures.ThreadPoolExecutor=ThreadPoolExecutor

