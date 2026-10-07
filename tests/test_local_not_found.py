import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from retry_proxy.api import create_handlers
from retry_proxy.routes import is_proxy_api_path


def request_for(path, method="GET", headers=()):
    return Request({
        "type": "http", "method": method, "path": "/" + path,
        "headers": headers, "query_string": b"",
        "server": ("test", 80), "client": ("127.0.0.1", 1234),
    })


class LocalNotFoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_local_paths_and_methods_never_reach_upstream(self):
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, None)[-1]
        with patch("retry_proxy.api.match_route") as route:
            for path, method in (
                ("requests/missing", "GET"), ("requests/", "GET"),
                ("admin/not-found", "POST"), ("settings/missing", "GET"),
                ("logs/missing", "GET"), ("stats", "DELETE"),
                ("key-pools/missing", "GET"), ("docs/missing", "GET"),
            ):
                with self.subTest(path=path, method=method):
                    response = await proxy(path, request_for(path, method))
                    self.assertEqual(response.status_code, 404)
            route.assert_not_called()
        service.request.assert_not_awaited()

    async def test_browser_unknown_page_is_local_404(self):
        proxy = create_handlers(SimpleNamespace(request=AsyncMock()), None)[-1]
        with patch("retry_proxy.api.match_route") as route:
            for headers in (
                [(b"accept", b"text/html,application/xhtml+xml")],
                [(b"sec-fetch-mode", b"navigate")],
            ):
                response = await proxy("404", request_for("404", headers=headers))
                self.assertEqual(response.status_code, 404)
            route.assert_not_called()

    async def test_api_requests_still_use_upstream_routing(self):
        proxy = create_handlers(SimpleNamespace(), None)[-1]
        for path, method, headers in (
            ("ocg/responses", "POST", [(b"accept", b"*/*")]),
            ("v1/models", "GET", [(b"accept", b"application/json")]),

        ):
            with self.subTest(path=path):
                with patch("retry_proxy.api.match_route",
                           side_effect=RuntimeError("routing reached")):
                    with self.assertRaisesRegex(RuntimeError, "routing reached"):
                        await proxy(path, request_for(path, method, headers))


class ProxyApiPathTests(unittest.TestCase):
    def test_supported_api_endpoints(self):
        for path in ("v1/responses", "responses/resp_123/cancel",
                     "v1/chat/completions", "v1/models", "v1/messages/count_tokens",
                     "v1/images/edits", "v1/audio/speech", "v1/files/file_123/content",
                     "v1beta/models/gemini:generateContent", "v1/sub2api/billing"):
            self.assertTrue(is_proxy_api_path(path), path)

    def test_arbitrary_paths_are_not_proxy_endpoints(self):
        for path in ("", "404", "unknown", "arbitrary/responses", "foo.html",
                     "chat/unknown", "v1/not-found", "images/unknown",
                     "v1/sub2api/unknown", "v1/sub2api/billing/unknown"):
            self.assertFalse(is_proxy_api_path(path), path)


class UnknownProxyPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_paths_rejected_after_route_prefix_removal(self):
        service = SimpleNamespace(request=AsyncMock())
        proxy = create_handlers(service, None)[-1]
        for path in ("unknown", "ocg/not-found", "v1/not-found"):
            for method in ("GET", "POST", "HEAD", "OPTIONS"):
                with self.subTest(path=path, method=method):
                    with patch("retry_proxy.api.match_route",
                               return_value=("https://upstream.test", "test", "not-found")):
                        response = await proxy(path, request_for(path, method))
                    self.assertEqual(response.status_code, 404)
        service.request.assert_not_awaited()
