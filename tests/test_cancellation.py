import asyncio
import unittest
from types import SimpleNamespace

from retry_proxy.api import _run_until_disconnect
from retry_proxy.retry import RetryProxy


class _DisconnectedRequest:
    async def is_disconnected(self):
        return True


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_proxy_work(self):
        cancelled = asyncio.Event()

        async def work():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        result = await _run_until_disconnect(_DisconnectedRequest(), work())

        self.assertIsNone(result)
        self.assertTrue(cancelled.is_set())

    async def test_race_cancellation_cleans_up_all_inflight_sends(self):
        config = SimpleNamespace(hedge_mode="race", max_concurrent=3, max_retries=0)
        proxy = RetryProxy(config=config, client=object())
        started = 0
        cancelled = 0
        all_started = asyncio.Event()

        async def send(*_args):
            nonlocal started, cancelled
            started += 1
            if started == config.max_concurrent:
                all_started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled += 1

        proxy._send = send
        task = asyncio.create_task(proxy.request("POST", "https://upstream.test", {}, b"{}",
                                                 "v1/chat", "test", "model"))
        await asyncio.wait_for(all_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(started, 3)
        self.assertEqual(cancelled, 3)


if __name__ == "__main__":
    unittest.main()
