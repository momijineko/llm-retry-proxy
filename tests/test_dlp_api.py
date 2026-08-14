import asyncio
import base64
import gzip
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

from retry_proxy.api import create_handlers
from tests.asyncio_compat import ThreadedAsyncTestCase


RULE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "retry_proxy", "dlp_rules.yaml",
)


def _config(**overrides):
    values = {
        "dlp_mode": "block",
        "dlp_rules": frozenset({"ai_tokens"}),
        "dlp_rule_file": RULE_FILE,
        "dlp_exempt_start": "[[ALLOW_SENSITIVE]]",
        "dlp_exempt_end": "[[/ALLOW_SENSITIVE]]",
        "dlp_allow_exemptions": False,
        "dlp_strip_exempt_markers": True,
        "dlp_max_body_bytes": 1024 * 1024,
        "dlp_decode_depth": 2,
        "dlp_decode_max_candidates": 100,
        "dlp_decode_max_bytes": 1024 * 1024,
        "dlp_known_secret_min_length": 8,
        "dlp_fail_closed": False,
        "proxy_api_key": "",
        "image_upstream_user_agent": "",
        "image_upstream_originator": "",
        "max_request_body": 64 * 1024 * 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(body, content_length=True, extra_headers=()):
    headers = [(b"content-type", b"application/json"), *list(extra_headers)]
    if content_length:
        headers.append((b"content-length", str(len(body)).encode()))

    async def receive():
        # 立即返回的协程会让 _run_until_disconnect 的断连监视器零让步忙等，
        # 事件循环永远轮不到转发任务；显式让出一次控制权
        await asyncio.sleep(0)
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/responses",
        "headers": headers,
        "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
    }, receive=receive)


class DlpApiTests(ThreadedAsyncTestCase):
    async def call_proxy(self, body, config, pools=None):
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, SimpleNamespace())[-1]
        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", pools or {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy("responses", _request(body))
        return response, service

    async def test_encoded_tool_output_is_blocked_before_upstream(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        encoded = base64.b64encode(token.encode()).decode()
        body = ('{"input":[{"type":"local_shell_call_output","output":"'
                + encoded + '"}]}').encode()

        response, service = await self.call_proxy(body, _config())

        self.assertEqual(response.status_code, 422)
        self.assertIn(b"sensitive_data_blocked", response.body)
        service.request.assert_not_awaited()

    async def test_encoded_unknown_key_pool_secret_is_blocked(self):
        secret = "vendor-private-value-987654321"
        encoded = base64.b64encode(secret.encode()).decode()
        pool = SimpleNamespace(entries=[SimpleNamespace(key=secret)])
        body = ('{"input":"' + encoded + '"}').encode()

        response, service = await self.call_proxy(
            body, _config(), {"https://upstream.test": pool},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(b"known_secret", response.body)
        service.request.assert_not_awaited()

    async def test_decode_budget_exhaustion_fails_closed(self):
        body = b'{"input":"QUFBQUFBQUFBQUFBQUFBQQ== QkJCQkJCQkJCQkJCQkJCQg=="}'

        response, service = await self.call_proxy(
            body, _config(dlp_decode_max_candidates=1),
        )

        self.assertEqual(response.status_code, 413)
        self.assertIn(b"dlp_decode_limit_exceeded", response.body)
        service.request.assert_not_awaited()

    async def test_uninspectable_body_can_fail_closed(self):
        response, service = await self.call_proxy(
            b"not-json", _config(dlp_fail_closed=True),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(b"dlp_uninspectable_body", response.body)
        service.request.assert_not_awaited()

    async def test_oversized_body_is_rejected_before_upstream(self):
        body = b'{"input":"' + b"x" * 200 + b'"}'
        response, service = await self.call_proxy(
            body, _config(max_request_body=100),
        )

        self.assertEqual(response.status_code, 413)
        self.assertIn(b"request_body_too_large", response.body)
        service.request.assert_not_awaited()

    async def test_oversized_body_without_content_length_is_rejected(self):
        body = b'{"input":"' + b"x" * 200 + b'"}'
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, SimpleNamespace())[-1]
        with patch("retry_proxy.api.settings", _config(max_request_body=100)), \
                patch("retry_proxy.config.settings", _config(max_request_body=100)), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy("responses", _request(body, content_length=False))

        self.assertEqual(response.status_code, 413)
        self.assertIn(b"request_body_too_large", response.body)
        service.request.assert_not_awaited()

    async def test_chunked_oversized_body_stops_reading_at_limit(self):
        receive = AsyncMock(side_effect=[
            {"type": "http.request", "body": b"x" * 60, "more_body": True},
            {"type": "http.request", "body": b"y" * 60, "more_body": True},
            {"type": "http.request", "body": b"z" * 10_000, "more_body": False},
        ])
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=receive)
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, SimpleNamespace())[-1]
        with patch("retry_proxy.api.settings", _config(max_request_body=100)), \
                patch("retry_proxy.config.settings", _config(max_request_body=100)), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy("responses", request)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(receive.await_count, 2)
        service.request.assert_not_awaited()

    async def test_gzip_body_is_decoded_before_dlp_and_blocked(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        payload = ('{"input":"' + token + '"}').encode()
        body = gzip.compress(payload)
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, SimpleNamespace())[-1]
        with patch("retry_proxy.api.settings", _config()), \
                patch("retry_proxy.config.settings", _config()), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy(
                "responses",
                _request(body, extra_headers=[(b"content-encoding", b"gzip")]),
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn(b"sensitive_data_blocked", response.body)
        service.request.assert_not_awaited()

    async def test_gzip_body_is_forwarded_plain_without_encoding_header(self):
        payload = b'{"input":"benign-content"}'
        body = gzip.compress(payload)
        upstream_response = httpx.Response(
            200, content=b'{"ok":true}',
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=1, total_sent=1,
            last_status=200, retry_codes=[], first_ok=True, key_id="",
            key_attempts=[], started_at=time.time(), key_entry=None,
            response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=AsyncMock(return_value=result),
            hedge_mode_for=lambda request_pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy = create_handlers(service, store)[-1]
        config = _config(dlp_mode="redact")
        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy(
                "responses",
                _request(body, extra_headers=[(b"content-encoding", b"gzip")]),
            )
            await response.body_iterator.__anext__()

        args = service.request.await_args.args
        forwarded_headers, forwarded_body = args[2], args[3]
        self.assertEqual(forwarded_body, payload)
        self.assertNotIn("content-encoding",
                         {name.lower() for name in forwarded_headers})

    async def test_gzip_decode_exceeding_dlp_limit_is_rejected(self):
        payload = b'{"input":"' + b"x" * 2048 + b'"}'
        body = gzip.compress(payload)
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, SimpleNamespace())[-1]
        with patch("retry_proxy.api.settings", _config(dlp_max_body_bytes=512)), \
                patch("retry_proxy.config.settings", _config(dlp_max_body_bytes=512)), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "responses")):
            response = await proxy(
                "responses",
                _request(body, extra_headers=[(b"content-encoding", b"gzip")]),
            )

        self.assertEqual(response.status_code, 413)
        self.assertIn(b"dlp_body_too_large", response.body)
        service.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
