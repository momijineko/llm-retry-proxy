"""管理会话与登录限速测试"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

import retry_proxy.admin_session as admin_session
from retry_proxy.admin_session import create_session, is_valid, revoke
from retry_proxy.application import _login_failure, _login_locked, _login_success


class AdminSessionTests(unittest.TestCase):
    def test_created_session_is_valid(self):
        token = create_session()
        self.assertTrue(is_valid(token))

    def test_unknown_or_empty_token_is_invalid(self):
        self.assertFalse(is_valid(""))
        self.assertFalse(is_valid("not-a-real-token"))

    def test_revoked_session_is_invalid(self):
        token = create_session()
        revoke(token)
        self.assertFalse(is_valid(token))

    def test_expired_session_is_invalid(self):
        ttl = admin_session.SESSION_TTL_SECONDS
        with patch.object(admin_session.time, "time",
                          side_effect=[1000.0, 1000.0, 1000.0 + ttl + 1]):
            token = create_session()
            self.assertFalse(is_valid(token))

    def test_tokens_are_random(self):
        first = create_session()
        second = create_session()
        self.assertNotEqual(first, second)
        self.assertGreater(len(first), 32)


class LoginRateLimitTests(unittest.TestCase):
    def setUp(self):
        # 清理其他测试类可能遗留的登录状态（unittest 按类名字母序执行）
        _login_success("203.0.113.7")
        self.patch = patch("retry_proxy.application.settings", SimpleNamespace(
            admin_login_max_attempts=3, admin_login_lockout_seconds=60,
        ))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(admin_session._sessions.clear)
        self.addCleanup(lambda: _login_success("203.0.113.7"))

    def test_failures_below_threshold_do_not_lock(self):
        for _ in range(2):
            _login_failure("203.0.113.7", 100.0)
        self.assertEqual(_login_locked("203.0.113.7", 100.0), 0.0)

    def test_threshold_triggers_lockout(self):
        for _ in range(3):
            _login_failure("203.0.113.7", 100.0)
        remaining = _login_locked("203.0.113.7", 100.0)
        self.assertAlmostEqual(remaining, 60.0)

    def test_lockout_expires(self):
        for _ in range(3):
            _login_failure("203.0.113.7", 100.0)
        self.assertEqual(_login_locked("203.0.113.7", 160.0), 0.0)

    def test_repeat_lockout_doubles_duration(self):
        for _ in range(3):
            _login_failure("203.0.113.7", 0.0)
        self.assertAlmostEqual(_login_locked("203.0.113.7", 0.0), 60.0)
        # 锁定期结束后再次失败，下一次锁定时长翻倍
        _login_failure("203.0.113.7", 60.0)
        _login_failure("203.0.113.7", 60.0)
        _login_failure("203.0.113.7", 60.0)
        self.assertAlmostEqual(_login_locked("203.0.113.7", 60.0), 120.0)

    def test_success_resets_failures(self):
        for _ in range(2):
            _login_failure("203.0.113.7", 100.0)
        _login_success("203.0.113.7")
        for _ in range(2):
            _login_failure("203.0.113.7", 100.0)
        self.assertEqual(_login_locked("203.0.113.7", 100.0), 0.0)

    def test_limit_disabled_when_threshold_zero(self):
        self.patch.stop()
        with patch("retry_proxy.application.settings", SimpleNamespace(
                admin_login_max_attempts=0, admin_login_lockout_seconds=60)):
            for _ in range(10):
                _login_failure("203.0.113.7", 100.0)
            self.assertEqual(_login_locked("203.0.113.7", 100.0), 0.0)
        self.patch.start()


class AdminLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _login_success("203.0.113.7")
        self.addCleanup(lambda: _login_success("203.0.113.7"))

    def _login_request(self, body=b"password=wrong&next=/stats"):
        request = Request({
            "type": "http", "method": "POST", "path": "/admin/login",
            "headers": [], "query_string": b"", "server": ("test", 80),
            "client": ("203.0.113.7", 1),
        })
        request.body = AsyncMock(return_value=body)
        return request

    async def test_locked_ip_gets_429_without_password_check(self):
        from retry_proxy.application import admin_login

        with patch("retry_proxy.application.settings", SimpleNamespace(
                admin_password="correct", trusted_proxy_ips=(),
                admin_login_max_attempts=2, admin_login_lockout_seconds=60,
                admin_cookie_secure=False)):
            request = self._login_request()
            for _ in range(2):
                response = await admin_login(request)
                self.assertEqual(response.status_code, 200)
            response = await admin_login(request)
            self.assertEqual(response.status_code, 429)
            self.assertIn("尝试次数过多", response.body.decode("utf-8"))

    async def test_correct_password_issues_random_session_cookie(self):
        from retry_proxy.application import admin_login

        with patch("retry_proxy.application.settings", SimpleNamespace(
                admin_password="correct", trusted_proxy_ips=(),
                admin_login_max_attempts=2, admin_login_lockout_seconds=60,
                admin_cookie_secure=False)):
            request = self._login_request(b"password=correct&next=/stats")
            response = await admin_login(request)
            self.assertEqual(response.status_code, 303)
            cookie = response.headers["set-cookie"]
            token = cookie.split("admin_session=", 1)[1].split(";", 1)[0]
            self.assertTrue(is_valid(token))
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)


if __name__ == "__main__":
    unittest.main()
