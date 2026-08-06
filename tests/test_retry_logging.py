import asyncio
import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from retry_proxy.config import LogCaptureHandler, log_capture, logger
from retry_proxy.key_pool import KeyEntry, KeyPool
from retry_proxy.retry import RequestAttemptBudget, RetryProxy, capped_retry_after


class RetryLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_attempt_budget_caps_internal_retries(self):
        config = SimpleNamespace(
            hedge_mode="off", max_retries=10, retry_interval=0,
            retry_interval_429=0, retry_backoff=False,
            retry_backoff_429=False, retry_backoff_max=0,
            retry_backoff_max_429=0,
        )
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(side_effect=lambda *_args: httpx.Response(
            503, request=httpx.Request("POST", "https://upstream.test/responses"),
        ))

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "v1/responses", "test", "model",
            max_attempts=10, attempt_budget=RequestAttemptBudget(1),
        )

        self.assertEqual(proxy._send.await_count, 1)
        self.assertEqual(result.total_sent, 1)
        self.assertEqual(result.failure_reason, "upstream attempt budget exhausted")

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

    async def test_key_pool_log_tag_is_separated_from_model_tag(self):
        config = SimpleNamespace(hedge_mode="off", max_retries=1)
        trace_logger = Mock()
        pool = KeyPool([("pool-key", "pool-key")])
        response = httpx.Response(
            200, request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        proxy = RetryProxy(config=config, client=object(), logger_=trace_logger)
        proxy._send = AsyncMock(return_value=response)

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {}, b"{}",
            "aihub/responses", "test", "model", pool,
        )

        self.assertEqual(result.key_id, "pool-key")
        messages = [
            LogCaptureHandler._ANSI_RE.sub("", call.args[0])
            for method in (trace_logger.debug, trace_logger.info)
            for call in method.call_args_list
        ]
        self.assertTrue(any(
            "[test/model] [pool-key]" in message for message in messages
        ))
        self.assertFalse(any(
            "[test/model][pool-key]" in message for message in messages
        ))

    async def test_deferred_stream_success_does_not_establish_key_sticky_state(self):
        config = SimpleNamespace(hedge_mode="off", max_retries=1)
        pool = KeyPool([("only", "only")])
        entry = pool.entries[0]
        proxy = RetryProxy(config=config, client=object())
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        proxy._send = AsyncMock(return_value=response)

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "v1/responses", "test", "model", pool,
            defer_stream_success=True,
        )

        self.assertIsNone(pool._current)
        self.assertEqual(result.key_attempts, [{
            "key_id": entry.key_id, "available": None,
        }])

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
        self.assertEqual(pool.entries[0].cooldown_until, 0)
        self.assertIsNone(result.key_attempts[0]["available"])

    async def test_non_cloudflare_service_unavailable_still_cools_key(self):
        config = SimpleNamespace(
            responses_header_timeout=1, responses_attempt_header_timeout=0,
            hedge_mode="off", max_retries=2, retry_interval=0,
            retry_interval_429=0, retry_backoff=False,
            retry_backoff_429=False, retry_backoff_max=0,
            retry_backoff_max_429=0, key_pool_wait_timeout=1,
            key_cooldown=30, key_cooldown_5xx=30,
            key_cooldown_backoff=False, key_cooldown_max=60,
        )
        pool = KeyPool([("first", "first"), ("good", "good")])
        proxy = RetryProxy(config=config, client=object())
        responses = [
            httpx.Response(
                503,
                json={"error": {"message": "Service temporarily unavailable"}},
                headers={"server": "upstream"},
                request=httpx.Request("POST", "https://upstream.test"),
            ),
            httpx.Response(
                200,
                request=httpx.Request("POST", "https://upstream.test"),
            ),
        ]
        proxy._send = AsyncMock(side_effect=responses)

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "aihub/responses", "test", "model", pool,
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertGreater(pool.entries[0].cooldown_until, time.time())

    async def test_large_streaming_responses_skips_attempt_timeout(self):
        config = SimpleNamespace(
            responses_header_timeout=1,
            responses_attempt_header_timeout=0.001,
            responses_attempt_header_timeout_body_limit=32,
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
        body = b'{"model":"model","stream":true,"input":"' + b"x" * 64 + b'"}'
        result = await proxy.request(
            "POST", "https://upstream.test/responses", {}, body,
            "aihub/responses", "test", "model", pool,
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(result.total_sent, 1)
        self.assertEqual(pool.entries[0].cooldown_until, 0)

    async def test_html_400_switches_pool_key(self):
        config = SimpleNamespace(
            responses_header_timeout=1, responses_attempt_header_timeout=1,
            hedge_mode="off", max_retries=2, retry_interval=0,
            retry_interval_429=0, retry_backoff=False, retry_backoff_max=0,
            retry_backoff_429=False, retry_backoff_max_429=0,
            key_cooldown=30, key_cooldown_5xx=30,
            key_cooldown_backoff=False, key_cooldown_max=60,
            key_pool_wait_timeout=1,
        )
        pool = KeyPool([])
        pool.entries = [KeyEntry("html-error", "html-error"), KeyEntry("good", "good")]
        pool.finalize_entries()
        responses = [
            httpx.Response(
                400, text="<html><title>400 Bad Request</title></html>",
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("POST", "https://upstream.test/responses"),
            ),
            httpx.Response(
                200, request=httpx.Request("POST", "https://upstream.test/responses"),
            ),
        ]
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(side_effect=responses)

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "aihub/responses", "test", "model", pool,
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(result.total_sent, 2)
        self.assertEqual(result.key_id, "good")
        self.assertEqual(result.retry_codes, [400])
        self.assertEqual(pool.entries[0].last_failure_status, 400)

    async def test_json_400_is_still_returned_without_switching_pool_key(self):
        config = SimpleNamespace(
            responses_header_timeout=1, responses_attempt_header_timeout=1,
            hedge_mode="off", max_retries=2,
        )
        pool = KeyPool(["first", "second"])
        response = httpx.Response(
            400, json={"error": {"message": "invalid request"}},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(return_value=response)

        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "aihub/responses", "test", "model", pool,
        )

        self.assertIs(result.response, response)
        self.assertEqual(result.total_sent, 1)
        self.assertEqual(result.retry_codes, [])
        self.assertEqual(proxy._send.await_count, 1)

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

    async def test_deferred_stream_success_uses_bridge_timeout_instead_of_header_timeout(self):
        config = SimpleNamespace(
            responses_header_timeout=0.001, responses_attempt_header_timeout=0.001,
            hedge_mode="off", max_retries=1,
        )
        pool = KeyPool([("only", "only")])
        proxy = RetryProxy(config=config, client=object())

        async def delayed_response(*_args):
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                request=httpx.Request("POST", "https://upstream.test/responses"),
            )

        proxy._send = delayed_response
        result = await proxy.request(
            "POST", "https://upstream.test/responses", {},
            b'{"model":"model","stream":true}',
            "responses", "test", "model", pool,
            defer_stream_success=True,
        )

        self.assertEqual(result.response.status_code, 200)
        self.assertEqual(result.total_sent, 1)
        self.assertEqual(pool.entries[0].cooldown_until, 0)

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


class RetryAfterCapTests(unittest.TestCase):
    # RETRY_AFTER_MAX 封顶逻辑：0 不封顶，正数对 Retry-After 解析值封顶

    def test_cap_zero_returns_parsed_value_uncapped(self):
        self.assertEqual(capped_retry_after("38318", 0), 38318.0)
        self.assertEqual(capped_retry_after("5", 0), 5.0)

    def test_cap_positive_limits_large_retry_after(self):
        self.assertEqual(capped_retry_after("38318", 600), 600.0)
        self.assertEqual(capped_retry_after("300", 600), 300.0)

    def test_cap_empty_value_returns_none(self):
        self.assertIsNone(capped_retry_after("", 600))
        self.assertIsNone(capped_retry_after(None, 600))

    def test_cap_does_not_alter_negative_retry_after(self):
        # 负数/非法值解析失败返回 None，不因封顶而误判
        self.assertIsNone(capped_retry_after("abc", 600))


class RetryAfterCapRequestTests(unittest.IsolatedAsyncioTestCase):
    # 429 带超大 Retry-After 时，RETRY_AFTER_MAX 应同时封顶重试等待与 key 熔断

    async def test_large_retry_after_is_capped_for_both_wait_and_cooldown(self):
        config = SimpleNamespace(
            hedge_mode="off", max_retries=2, retry_interval=0,
            retry_interval_429=5, retry_backoff=False,
            retry_backoff_429=False, retry_backoff_max=0,
            retry_backoff_max_429=60, retry_after_max=600,
            key_cooldown=30, key_cooldown_5xx=30,
            key_cooldown_429=60, key_cooldown_auth=1800,
            key_cooldown_backoff=False, key_cooldown_max=3600,
            key_pool_wait_timeout=0.5,
        )
        pool = KeyPool([("key", "key")])
        proxy = RetryProxy(config=config, client=object())
        sleep_wait = []

        async def send(*_args):
            return httpx.Response(
                429, headers={"retry-after": "38318"},
                request=httpx.Request("POST", "https://upstream.test"),
            )

        async def fake_sleep(wait, _pool=None, _pool_wait=0.0, _wait_timeout=None):
            sleep_wait.append(wait)
            await asyncio.sleep(0)

        proxy._send = send
        with patch("retry_proxy.retry._sleep_before_retry", new=fake_sleep), \
                patch("retry_proxy.retry.time.time", return_value=1000):
            result = await proxy.request(
                "POST", "https://upstream.test", {}, b"{}",
                "v1/chat", "test", "model", pool,
            )

        self.assertIsNone(result.response)
        # 重试等待被封顶到 600s，而非上游的 38318s
        self.assertTrue(sleep_wait)
        self.assertLessEqual(sleep_wait[0], 600)
        # key 熔断被同样封顶：cooldown_until 不超过 当前时间 + 600s
        self.assertLessEqual(pool.entries[0].cooldown_until, 1000 + 600)

    async def test_zero_cap_keeps_uncapped_retry_after(self):
        config = SimpleNamespace(
            hedge_mode="off", max_retries=2, retry_interval=0,
            retry_interval_429=5, retry_backoff=False,
            retry_backoff_429=False, retry_backoff_max=0,
            retry_backoff_max_429=60, retry_after_max=0,
            key_cooldown=30, key_cooldown_5xx=30,
            key_cooldown_429=60, key_cooldown_auth=1800,
            key_cooldown_backoff=False, key_cooldown_max=3600,
            key_pool_wait_timeout=0.5,
        )
        pool = KeyPool([("key", "key")])
        proxy = RetryProxy(config=config, client=object())
        sleep_wait = []

        async def send(*_args):
            return httpx.Response(
                429, headers={"retry-after": "38318"},
                request=httpx.Request("POST", "https://upstream.test"),
            )

        async def fake_sleep(wait, _pool=None, _pool_wait=0.0, _wait_timeout=None):
            sleep_wait.append(wait)
            await asyncio.sleep(0)

        proxy._send = send
        with patch("retry_proxy.retry._sleep_before_retry", new=fake_sleep), \
                patch("retry_proxy.retry.time.time", return_value=1000):
            result = await proxy.request(
                "POST", "https://upstream.test", {}, b"{}",
                "v1/chat", "test", "model", pool,
            )

        self.assertIsNone(result.response)
        self.assertTrue(sleep_wait)
        # 默认行为保持不变：完全尊重上游
        self.assertGreater(sleep_wait[0], 600)
        self.assertGreater(pool.entries[0].cooldown_until, 1000 + 600)


if __name__ == "__main__":
    unittest.main()
