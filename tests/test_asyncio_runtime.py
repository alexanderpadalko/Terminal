"""Reproduce the user's frozen lazy_import failure before applying the fix."""
import asyncio
import concurrent.futures
import socket
import unittest
from unittest.mock import patch

from flowdesk.asyncio_runtime import prepare_asyncio_runtime


class TestAsyncioRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_export_cannot_create_executor_then_bootstrap_repairs_dns(self):
        loop=asyncio.get_running_loop()
        unresolved=object()
        with patch.object(concurrent.futures,'ThreadPoolExecutor',unresolved):
            with self.assertRaises(TypeError):
                loop.run_in_executor(None,lambda:17)
            prepare_asyncio_runtime()
            with patch('socket.getaddrinfo',return_value=[('resolved',)]) as lookup:
                result=await loop.getaddrinfo('iss.moex.com',443,type=socket.SOCK_STREAM)
            self.assertEqual(result,[('resolved',)])
            lookup.assert_called_once()
            self.assertEqual(await loop.run_in_executor(None,lambda:17),17)

    async def test_bootstrap_preserves_existing_executor_and_is_idempotent(self):
        loop=asyncio.get_running_loop()
        prepare_asyncio_runtime()
        self.assertEqual(await loop.run_in_executor(None,lambda:23),23)
        existing=loop._default_executor
        prepare_asyncio_runtime()
        self.assertIs(loop._default_executor,existing)
        self.assertEqual(await loop.run_in_executor(None,lambda:29),29)
