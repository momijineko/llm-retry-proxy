import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from retry_proxy.config import log_capture, logger
from retry_proxy.retry import RetryProxy


class RetryLoggingTests(unittest.IsolatedAsyncioTestCase):
    def test_forward_debug_reaches_log_page_capture(self):
        marker = "retry-debug-capture-test"

        logger.debug(marker)

        matches = [entry for entry in log_capture.history() if entry["message"] == marker]
        self.assertTrue(matches)
        self.assertEqual(matches[-1]["level"], "DEBUG")
        self.assertEqual(logger.getEffectiveLevel(), logging.DEBUG)

    async def test_off_mode_logs_request_send_and_response_header_stages(self):
        config = SimpleNamespace(hedge_mode="off", max_retries=2)
        trace_logger = Mock()
        response = SimpleNamespace(status_code=200, headers={})
        proxy = RetryProxy(config=config, client=object(), logger_=trace_logger)
        proxy._send = AsyncMock(return_value=response)

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {}, b"{}",
            "responses", "test", "model",
        )

        self.assertIs(result.response, response)
        messages = [call.args[0] for call in trace_logger.debug.call_args_list]
        self.assertTrue(any("开始转发" in message for message in messages))
        self.assertTrue(any("#1 选号" in message for message in messages))
        self.assertTrue(any("#1 发出上游" in message for message in messages))
        self.assertTrue(any("#1 收到响应头 200" in message for message in messages))

    async def test_responses_header_wait_has_a_hard_timeout(self):
        config = SimpleNamespace(
            responses_header_timeout=0.01, hedge_mode="off", max_retries=1,
        )
        proxy = RetryProxy(config=config, client=object())

        async def never_returns(*_args):
            await asyncio.Future()

        proxy._send = never_returns
        result = await proxy.request(
            "POST", "https://upstream.test/responses", {}, b"{}",
            "aihub/responses", "test", "model",
        )

        self.assertIsNone(result.response)
        self.assertEqual(result.total_sent, 1)
        self.assertIn("within 0.0s", result.failure_reason)

    async def test_stagger_retries_are_spacing_gated_after_503(self):
        config = SimpleNamespace(
            hedge_mode="stagger", max_concurrent=10, max_retries=3,
            retry_interval=0.03, retry_interval_429=0.05,
            retry_backoff=False, retry_backoff_max=60,
            retry_backoff_429=True, retry_backoff_max_429=60,
        )
        proxy = RetryProxy(config=config, client=object())
        sent_at = []

        async def send(*_args):
            sent_at.append(time.monotonic())
            return httpx.Response(
                503, json={"error": {"message": "temporarily unavailable"}},
                request=httpx.Request("POST", "https://upstream.test"),
            )

        proxy._send = send
        result = await proxy.request(
            "POST", "https://upstream.test", {}, b"{}",
            "v1/chat", "test", "model",
        )

        self.assertIsNone(result.response)
        self.assertEqual(len(sent_at), 3)
        self.assertGreaterEqual(min(b - a for a, b in zip(sent_at, sent_at[1:])), 0.02)

    async def test_stagger_initial_launches_are_spacing_gated(self):
        config = SimpleNamespace(
            hedge_mode="stagger", max_concurrent=3, max_retries=3,
            retry_interval=0.03, retry_interval_429=0.05,
            retry_backoff=False, retry_backoff_max=60,
            retry_backoff_429=True, retry_backoff_max_429=60,
        )
        proxy = RetryProxy(config=config, client=object())
        sent_at = []

        async def send(*_args):
            sent_at.append(time.monotonic())
            await asyncio.sleep(0.08)
            return httpx.Response(
                200, json={"ok": True},
                request=httpx.Request("POST", "https://upstream.test"),
            )

        proxy._send = send
        result = await proxy.request(
            "POST", "https://upstream.test", {}, b"{}",
            "v1/chat", "test", "model",
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(len(sent_at), 3)
        self.assertGreaterEqual(min(b - a for a, b in zip(sent_at, sent_at[1:])), 0.02)


if __name__ == "__main__":
    unittest.main()
