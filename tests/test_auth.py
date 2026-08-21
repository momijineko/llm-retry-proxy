import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from retry_proxy.admin_session import create_session
from retry_proxy.config import (LogCaptureHandler, can_use_key_pool,
                                require_admin)
from retry_proxy.api import _responses_stream_key_failure_status, create_handlers
from retry_proxy.key_pool import KeyEntry, KeyPool


class _BlockingStream(httpx.AsyncByteStream):
    def __init__(self, first_chunk):
        self.first_chunk = first_chunk
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield self.first_chunk
        self.waiting.set()
        await asyncio.Future()


def _request(authorization="", cookie="", path="/stats/api"):
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request({"type": "http", "method": "GET", "path": path,
                    "headers": headers, "query_string": b"", "server": ("test", 80)})


class AdminAuthTests(unittest.TestCase):
    def test_unconfigured_token_disables_admin_endpoints(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="")):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request())
        self.assertEqual(raised.exception.status_code, 503)

    def test_invalid_token_is_rejected_with_bearer_challenge(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="correct")):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request("Bearer wrong"))
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.headers["WWW-Authenticate"], "Bearer")

    def test_valid_bearer_token_is_accepted(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="correct")):
            self.assertIsNone(require_admin(_request("Bearer correct")))

    def test_valid_session_cookie_is_accepted(self):
        fake_settings = SimpleNamespace(admin_password="correct")
        with patch("retry_proxy.config.settings", fake_settings):
            cookie = f"admin_session={create_session()}"
            self.assertIsNone(require_admin(_request(cookie=cookie)))

    def test_random_or_stale_cookie_is_rejected(self):
        fake_settings = SimpleNamespace(admin_password="correct")
        with patch("retry_proxy.config.settings", fake_settings):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request(cookie="admin_session=forged-value"))
        self.assertEqual(raised.exception.status_code, 401)

    def test_browser_page_redirects_to_login(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="correct")):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request(path="/logs"))
        self.assertEqual(raised.exception.status_code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/admin/login?next=/logs")

    def test_key_pool_page_redirects_to_login(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="correct")):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request(path="/key-pools"))
        self.assertEqual(raised.exception.status_code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/admin/login?next=/key-pools")


class ProxyPoolAuthTests(unittest.TestCase):
    def test_unconfigured_key_preserves_legacy_pool_access(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(proxy_api_key="")):
            self.assertTrue(can_use_key_pool({}))

    def test_matching_bearer_key_allows_pool_access(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(proxy_api_key="pool-secret")):
            self.assertTrue(can_use_key_pool({"authorization": "Bearer pool-secret"}))

    def test_missing_or_wrong_key_falls_back_to_plain_proxy(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(proxy_api_key="pool-secret")):
            self.assertFalse(can_use_key_pool({}))
            self.assertFalse(can_use_key_pool({"authorization": "Bearer wrong"}))


class ProxyPoolRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_responses_stream_records_cache_usage(self):
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        stream_body = (
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":4096,"input_tokens_details":{"cached_tokens":0}}}}\n\n'
        )
        upstream_response = httpx.Response(
            200, content=stream_body, headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        pool = KeyPool([("pool-key", "pool-key")])
        entry = pool.entries[0]
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=1, total_sent=1,
            last_status=200, retry_codes=[], first_ok=True, key_id=entry.key_id,
            key_attempts=[{"key_id": entry.key_id, "available": True}],
            started_at=time.time(), key_entry=entry,
            response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=lambda *args, **kwargs: None,
            hedge_mode_for=lambda request_pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        trace_logger = Mock()
        proxy = create_handlers(service, store)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request",
            "body": b'{"model":"grok-test","stream":true,"prompt_cache_key":"cache-1"}',
            "more_body": False,
        }))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.logger", trace_logger), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)), \
                patch.object(KeyPool, "record_cache_usage", autospec=True) as record_cache:
            response = await proxy("responses", request)
            body = b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(body, stream_body)
        record_cache.assert_called_once_with(ANY, entry, 4096, 0, "cache-1")
        info_messages = [
            LogCaptureHandler._ANSI_RE.sub("", call.args[0])
            for call in trace_logger.info.call_args_list
        ]
        self.assertTrue(any(
            "[test/grok-test] [pool-key] Responses流结束" in message
            for message in info_messages
        ))
        self.assertFalse(any(
            "[test/grok-test][pool-key]" in message for message in info_messages
        ))

    async def test_cancelled_responses_stream_is_logged_as_client_end(self):
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        upstream_stream = _BlockingStream(
            b'data: {"type":"response.output_item.done","item":{}}\n\n',
        )
        upstream_response = httpx.Response(
            200, stream=upstream_stream,
            headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        pool = KeyPool([("pool-key", "pool-key")])
        entry = pool.entries[0]
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=1, total_sent=1,
            last_status=200, retry_codes=[], first_ok=True,
            key_id=entry.key_id,
            key_attempts=[{"key_id": entry.key_id, "available": True}],
            started_at=time.time(), key_entry=entry,
            response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=lambda *args, **kwargs: None,
            hedge_mode_for=lambda request_pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        trace_logger = Mock()
        proxy = create_handlers(service, store)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request",
            "body": b'{"model":"grok-test","stream":true}',
            "more_body": False,
        }))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.logger", trace_logger), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)):
            response = await proxy("responses", request)
            iterator = response.body_iterator.__aiter__()
            await iterator.__anext__()
            next_chunk = asyncio.create_task(iterator.__anext__())
            await upstream_stream.waiting.wait()
            next_chunk.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await next_chunk

        record = store.write.await_args.args[0]
        self.assertEqual(record["stream_status"], "cancelled")
        self.assertFalse(record["succeeded"])
        self.assertIsNone(record["key_attempts"][-1]["available"])
        info_messages = [
            LogCaptureHandler._ANSI_RE.sub("", call.args[0])
            for call in trace_logger.info.call_args_list
        ]
        warning_messages = [
            LogCaptureHandler._ANSI_RE.sub("", call.args[0])
            for call in trace_logger.warning.call_args_list
        ]
        self.assertTrue(any(
            "[test/grok-test] [pool-key] Responses流客户端已结束" in message
            for message in info_messages
        ))
        self.assertFalse(any("Responses流失败" in message for message in warning_messages))

    async def test_responses_stream_embedded_502_does_not_cool_key(self):
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        stream_body = (
            b'event: response.created\ndata: {"type":"response.created"}\n\n'
            b'event: error\ndata: {"type":"error","error":{"status_code":502}}\n\n'
        )
        upstream_response = httpx.Response(
            200, content=stream_body, headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        pool = KeyPool([("pool-key", "pool-key")])
        entry = pool.entries[0]
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=1, total_sent=1,
            last_status=200, retry_codes=[], first_ok=True,
            key_id=entry.key_id,
            key_attempts=[{"key_id": entry.key_id, "available": True}],
            started_at=time.time(), key_entry=entry,
            response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=lambda *args, **kwargs: None,
            hedge_mode_for=lambda request_pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        trace_logger = Mock()
        proxy = create_handlers(service, store)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request",
            "body": b'{"model":"grok-test","stream":true}',
            "more_body": False,
        }))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.logger", trace_logger), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)):
            response = await proxy("responses", request)
            store.write.assert_not_awaited()
            body = b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(body, stream_body)
        store.write.assert_awaited_once()
        record = store.write.await_args.args[0]
        self.assertEqual(record["final_status"], 200)
        self.assertFalse(record["succeeded"])
        self.assertEqual(record["stream_status"], "error")
        self.assertEqual(record["stream_error_status"], 502)
        self.assertIsNone(record["key_attempts"][-1]["available"])
        self.assertEqual(entry.cooldown_until, 0)
        warning_messages = [
            LogCaptureHandler._ANSI_RE.sub("", call.args[0])
            for call in trace_logger.warning.call_args_list
        ]
        self.assertTrue(any(
            "[test/grok-test] [pool-key] Responses流失败" in message
            for message in warning_messages
        ))

    def test_only_explicit_stream_auth_and_rate_limit_errors_affect_key(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.assertEqual(
                    _responses_stream_key_failure_status("error", status),
                    status,
                )
        for stream_status, status in (
                ("error", None), ("error", 400), ("error", 500),
                ("error", 502), ("transport_error", 429),
                ("missing_terminal", None)):
            with self.subTest(stream_status=stream_status, status=status):
                self.assertIsNone(
                    _responses_stream_key_failure_status(stream_status, status),
                )

    async def test_real_model_not_found_response_updates_the_used_group(self):
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        entry = KeyEntry("group-key", "group", sort="0.01", group_id="free",
                         routing_capabilities={
                             "platform": "openai", "endpoint_families": ["responses"],
                             "model_patterns": ["gpt-example"], "model_list_known": True,
                         })
        pool = KeyPool([])
        pool.entries = [entry]
        pool.finalize_entries()
        upstream_response = httpx.Response(
            403, json={"error": {"code": "model_disabled",
                                  "message": "Model is not enabled for this group"}},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=1, total_sent=1,
            last_status=404, retry_codes=[], first_ok=True,
            key_id=entry.key_id, key_attempts=[], started_at=time.time(),
            key_entry=entry, response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=lambda *args, **kwargs: None,
            hedge_mode_for=lambda request_pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        pool_sync = SimpleNamespace(mark_model_unsupported=AsyncMock(return_value=True))
        proxy = create_handlers(service, store, pool_sync)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request", "body": b'{"model":"gpt-example"}',
            "more_body": False,
        }))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)):
            response = await proxy("responses", request)

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"model_disabled", response.body)
        pool_sync.mark_model_unsupported.assert_awaited_once_with(
            "https://upstream.test", "free", "gpt-example", "responses",
        )

    async def test_retried_model_rejection_updates_failed_group_after_success(self):
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        winner = KeyEntry("winner-key", "winner", group_id="paid")
        pool = KeyPool([])
        pool.entries = [winner]
        pool.finalize_entries()
        upstream_response = httpx.Response(
            200, json={"choices": []},
            request=httpx.Request("POST", "https://upstream.test/chat/completions"),
        )
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=2, total_sent=2,
            last_status=200, retry_codes=[403], first_ok=False,
            key_id=winner.key_id, key_attempts=[], started_at=time.time(),
            key_entry=winner, response_started_mono=time.monotonic(),
            model_rejections=["free"],
            model_rejection_routes=[("free", "chat")],
        )
        service = SimpleNamespace(
            request=lambda *args, **kwargs: None,
            hedge_mode_for=lambda request_pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        pool_sync = SimpleNamespace(mark_model_unsupported=AsyncMock(return_value=True))
        proxy = create_handlers(service, store, pool_sync)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/chat/completions",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request", "body": b'{"model":"luna"}',
            "more_body": False,
        }))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "chat/completions")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)):
            response = await proxy("chat/completions", request)
            body = b"".join([chunk async for chunk in response.body_iterator])

        self.assertIn(b"choices", body)
        pool_sync.mark_model_unsupported.assert_awaited_once_with(
            "https://upstream.test", "free", "luna", "chat",
        )

    async def test_matching_proxy_key_is_not_forwarded_when_pool_is_missing(self):
        config = SimpleNamespace(
            proxy_api_key="pool-secret", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        service = SimpleNamespace(request=AsyncMock())
        store = SimpleNamespace()
        proxy = create_handlers(service, store)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/aihub/responses",
            "headers": [(b"authorization", b"Bearer pool-secret")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={"type": "http.request", "body": b"{}"}))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy("aihub/responses", request)

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"key_pool_unavailable", response.body)
        service.request.assert_not_awaited()

    async def test_incompatible_synced_pool_is_rejected_without_upstream_request(self):
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
        )
        pool = KeyPool([])
        pool.entries = [KeyEntry("anthropic-key", "anthropic", routing_capabilities={
            "platform": "anthropic", "endpoint_families": ["messages"],
        })]
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, SimpleNamespace())[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request", "body": b'{"model":"gpt-4o"}', "more_body": False,
        }))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy("responses", request)

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"key_pool_no_compatible_route", response.body)
        self.assertIn(b'"endpoint_family": "responses"', response.body)
        service.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
