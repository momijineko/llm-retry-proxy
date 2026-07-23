import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from retry_proxy.config import LogCaptureHandler, log_capture, logger
from retry_proxy.key_pool import KeyEntry, KeyPool
from retry_proxy.retry import RetryProxy


class RetryLoggingTests(unittest.IsolatedAsyncioTestCase):
    def test_log_capture_keeps_all_entries_for_process_lifetime(self):
        capture = LogCaptureHandler()

        for index in range(2100):
            record = logging.LogRecord(
                "test", logging.INFO, __file__, 1, "entry %d", (index,), None,
            )
            capture.emit(record)

        history = capture.history()
        self.assertEqual(len(history), 2100)
        self.assertEqual(history[0]["message"], "entry 0")
        self.assertEqual(history[-1]["message"], "entry 2099")
        self.assertEqual(
            [entry["message"] for entry in capture.history(since=2098)],
            ["entry 2098", "entry 2099"],
        )

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

    async def test_streaming_responses_does_not_log_headers_as_completed(self):
        config = SimpleNamespace(hedge_mode="off", max_retries=1)
        trace_logger = Mock()
        response = SimpleNamespace(status_code=200, headers={})
        proxy = RetryProxy(config=config, client=object(), logger_=trace_logger)
        proxy._send = AsyncMock(return_value=response)

        await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "v1/responses", "test", "model",
        )

        messages = [call.args[0] for call in trace_logger.info.call_args_list]
        self.assertTrue(any("响应头已建立，等待Responses流结束" in message for message in messages))

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

    async def test_streaming_responses_attempt_timeout_switches_pool_key(self):
        config = SimpleNamespace(
            responses_header_timeout=1, responses_attempt_header_timeout=0.01,
            hedge_mode="off", max_retries=2, retry_interval=0,
            key_cooldown=30, key_cooldown_5xx=30,
            key_cooldown_backoff=False, key_cooldown_max=60,
            key_pool_wait_timeout=1,
        )
        pool = KeyPool([])
        pool.entries = [KeyEntry("slow", "slow"), KeyEntry("good", "good")]
        pool.finalize_entries()
        proxy = RetryProxy(config=config, client=object())
        cancelled = asyncio.Event()

        async def send(_method, _url, headers, _body):
            if headers.get("authorization") == "Bearer slow":
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return httpx.Response(
                200, request=httpx.Request("POST", "https://upstream.test"),
            )

        proxy._send = send
        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "aihub/responses", "test", "model", pool,
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(result.total_sent, 2)
        self.assertEqual(result.key_id, "good")
        self.assertTrue(cancelled.is_set())
        self.assertGreater(pool.entries[0].cooldown_until, time.time())

    async def test_non_streaming_responses_does_not_use_attempt_timeout(self):
        config = SimpleNamespace(
            responses_header_timeout=1, responses_attempt_header_timeout=0.001,
            hedge_mode="off", max_retries=1,
        )
        pool = KeyPool([("only", "only")])
        proxy = RetryProxy(config=config, client=object())

        async def delayed_response(*_args):
            await asyncio.sleep(0.01)
            return httpx.Response(
                200, request=httpx.Request("POST", "https://upstream.test"),
            )

        proxy._send = delayed_response
        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":false}',
            "responses", "test", "model", pool,
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(result.total_sent, 1)

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
