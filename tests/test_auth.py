import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from retry_proxy.config import admin_session_value, can_use_key_pool, require_admin


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


if __name__ == "__main__":
    unittest.main()
