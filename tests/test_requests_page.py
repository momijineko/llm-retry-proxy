import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

from retry_proxy.api import _inspectable_request_body, create_handlers


class RequestBodyDiagnosticTests(unittest.TestCase):
    def test_non_json_and_non_json_content_types_are_not_logged(self):
        self.assertIsNone(_inspectable_request_body(b"prompt=private", {"content-type": "text/plain"}))
        self.assertIsNone(_inspectable_request_body(b"not-json", {"content-type": "application/json"}))

    def test_binary_payload_fields_are_omitted(self):
        body = b'{"input":"inspect this","image":"base64-secret","file_data":"document-secret"}'
        diagnostic = _inspectable_request_body(body, {"content-type": "application/json"})

        self.assertEqual(diagnostic["input"], "inspect this")
        self.assertEqual(diagnostic["image"], "[omitted]")
        self.assertEqual(diagnostic["file_data"], "[omitted]")

    def test_missing_content_type_is_not_logged(self):
        self.assertIsNone(_inspectable_request_body(b'{"input":"prompt"}', {}))


class RequestDiagnosticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_api_filters_diagnostic_records_and_caps_limit(self):
        store = SimpleNamespace(load=lambda days, **kwargs: [
            {"ts": "2026-09-23T10:00:00", "request_body": {"input": "prompt"}},
            {"ts": "2026-09-23T10:01:00", "request_body_unavailable": True},
            {"ts": "2026-09-23T10:02:00", "model": "normal"},
        ])
        handlers = create_handlers(None, store)
        with patch("retry_proxy.api.settings", SimpleNamespace(request_body_logging=True)):
            result = await handlers[7](range="all", limit=10000)

        self.assertEqual(result["count"], 2)
        self.assertTrue(result["records"][0]["request_body_unavailable"])
        self.assertEqual(result["records"][1]["request_body"]["input"], "prompt")

    async def test_requests_api_is_disabled_by_default(self):
        handlers = create_handlers(None, SimpleNamespace(load=lambda _days: []))
        with patch("retry_proxy.api.settings", SimpleNamespace(request_body_logging=False)):
            result = await handlers[7]()

        self.assertEqual(result.status_code, 404)

    async def test_request_body_is_written_only_when_opted_in(self):
        response_result = SimpleNamespace(
            response=httpx.Response(200, content=b"ok", request=httpx.Request("POST", "https://upstream.test/responses")),
            winner_attempt=1, total_sent=1, last_status=200, retry_codes=[], first_ok=True,
            key_id="", key_attempts=[], started_at=time.time(), key_entry=None,
            response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=AsyncMock(return_value=response_result),
            hedge_mode_for=lambda _pool: "off",
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy = create_handlers(service, store)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": "/responses",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("203.0.113.9", 1234),
        }, receive=AsyncMock(return_value={
            "type": "http.request", "body": b'{"model":"test-model","input":"hello"}', "more_body": False,
        }))
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
            request_body_logging=True,
        )

        async def run_request(_request, awaitable):
            return await awaitable

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route", return_value=("https://upstream.test", "test", "responses")), \
                patch("retry_proxy.api._run_until_disconnect", side_effect=run_request):
            response = await proxy("responses", request)
            async for _chunk in response.body_iterator:
                pass

        record = store.write.await_args.args[0]
        self.assertEqual(record["request_body"], {"model": "test-model", "input": "hello"})
        self.assertEqual(record["client_ip"], "203.0.113.9")


class RequestDiagnosticsPageTests(unittest.TestCase):
    def test_page_contains_filtering_and_json_inspection_controls(self):
        html = (Path(__file__).resolve().parents[1] / "requests.html").read_text(encoding="utf-8")

        self.assertIn("/requests/api?range=", html)
        self.assertIn("查看 JSON", html)
        self.assertIn("客户端", html)
        self.assertIn("function esc(value)", html)


if __name__ == "__main__":
    unittest.main()
