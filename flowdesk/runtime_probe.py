"""Credential-free packaged regression for the Finam worker's asyncio startup."""
import asyncio
import concurrent.futures
import json
from pathlib import Path
import socket
import sys

from .finam_stream import FinamStreamWorker


def check_finam_runtime(output):
    result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    worker = FinamStreamWorker("TEST", "MISX")
    async def exercise_executor():
        result["executor_callable"] = callable(concurrent.futures.ThreadPoolExecutor)
        result["thread_result"] = await asyncio.to_thread(lambda: 17)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            "localhost", 443, type=socket.SOCK_STREAM)
        result["localhost_resolved"] = bool(addresses)
    worker._connect_loop = exercise_executor
    try:
        worker.run()
        result["ok"] = (result.get("executor_callable") and
                        result.get("thread_result") == 17 and result.get("localhost_resolved"))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
