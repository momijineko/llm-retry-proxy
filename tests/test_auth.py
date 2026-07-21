import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from retry_proxy.config import admin_session_value, can_use_key_pool, require_admin
from retry_proxy.api import create_handlers
from retry_proxy.key_pool import KeyEntry, KeyPool


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
            cookie = f"admin_session={admin_session_value()}"
            self.assertIsNone(require_admin(_request(cookie=cookie)))

    def test_browser_page_redirects_to_login(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="correct")):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request(path="/logs"))
        self.assertEqual(raised.exception.status_code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/admin/login?next=/logs")

    def test_key_pool_page_redirects_to_login(self):
        with patch("retry_proxy.config.settings", SimpleNamespace(admin_password="correct")):
            with self.assertRaises(HTTPException) as raised:
                require_admin(_request(path="/admin/key-pools"))
        self.assertEqual(raised.exception.status_code, 303)
        self.assertEqual(raised.exception.headers["Location"], "/admin/login?next=/admin/key-pools")


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
