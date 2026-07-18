import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from retry_proxy.api import create_handlers


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
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(body):
    return Request({
        "type": "http", "method": "POST", "path": "/responses",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
    }, receive=AsyncMock(return_value={
        "type": "http.request", "body": body, "more_body": False,
    }))


class DlpApiTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
