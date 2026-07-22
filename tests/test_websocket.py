import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.datastructures import Headers, QueryParams, URL
from starlette.websockets import WebSocketState

from retry_proxy.api import (
    _websocket_message_has_token,
    _websocket_request_info,
    _websocket_url,
    create_websocket_handler,
)
from retry_proxy.key_pool import KeyEntry, KeyPool


RULE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "retry_proxy", "dlp_rules.yaml",
)


class FakeDownstream:
    def __init__(self, messages, headers=None, query=""):
        self.headers = Headers(headers or {})
        self.query_params = QueryParams(query)
        self.url = URL("ws://proxy.test/responses" + (f"?{query}" if query else ""))
        self.client = SimpleNamespace(host="127.0.0.1")
        self.client_state = WebSocketState.CONNECTING
        self._messages = list(messages)
        self.sent = []
        self.close_code = None
        self.close_reason = ""

    async def accept(self, subprotocol=None):
        self.client_state = WebSocketState.CONNECTED

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.Event().wait()

    async def send_text(self, value):
        self.sent.append(value)

    async def send_bytes(self, value):
        self.sent.append(value)

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason
        self.client_state = WebSocketState.DISCONNECTED


class FakeUpstream:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False
        self.subprotocol = None

    async def send(self, value):
        self.sent.append(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self):
        self.closed = True


def _config(**overrides):
    values = {
        "proxy_api_key": "pool-access",
        "dlp_mode": "off",
        "key_pool_wait_timeout": 1,
        "connect_timeout": 1,
        "trust_env": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WebSocketProtocolTests(unittest.TestCase):
    def test_http_upstream_is_converted_to_websocket_url(self):
        self.assertEqual(
            _websocket_url("https://upstream.test/v1", "responses", "foo=bar"),
            "wss://upstream.test/v1/responses?foo=bar",
        )

    def test_response_create_exposes_model_and_skips_warmup(self):
        model, generates, recognized = _websocket_request_info(json.dumps({
            "type": "response.create", "model": "gpt-5", "generate": False,
        }))
        self.assertEqual(model, "gpt-5")
        self.assertFalse(generates)
        self.assertTrue(recognized)

    def test_metadata_and_error_frames_do_not_count_as_token(self):
        for event in (
            {"type": "response.created", "response": {"id": "resp-1"}},
            {"type": "response.failed", "response": {"id": "resp-1"}},
            {"type": "error", "message": "no token"},
        ):
            self.assertFalse(_websocket_message_has_token(json.dumps(event)))
        self.assertTrue(_websocket_message_has_token(json.dumps({
            "type": "response.output_text.delta", "delta": "hello",
        })))


class WebSocketProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_first_request_is_blocked_before_upstream_connect(self):
        config = _config(
            proxy_api_key="",
            dlp_mode="block",
            dlp_rules=frozenset({"ai_tokens"}),
            dlp_rule_file=RULE_FILE,
            dlp_exempt_start="[[ALLOW_SENSITIVE]]",
            dlp_exempt_end="[[/ALLOW_SENSITIVE]]",
            dlp_allow_exemptions=False,
            dlp_strip_exempt_markers=True,
            dlp_max_body_bytes=1024 * 1024,
            dlp_decode_depth=0,
            dlp_decode_max_candidates=100,
            dlp_decode_max_bytes=1024 * 1024,
            dlp_known_secret_min_length=8,
            dlp_fail_closed=False,
        )
        request = json.dumps({
            "type": "response.create", "model": "gpt-5",
            "input": "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx",
        })
        downstream = FakeDownstream([
            {"type": "websocket.receive", "text": request},
        ], {"authorization": "Bearer pool-access"})
        handler = create_websocket_handler(SimpleNamespace(write=AsyncMock()))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {}), \
                patch("retry_proxy.api.match_route", return_value=(
                    "https://upstream.test/v1", "test", "responses",
                )), \
                patch("retry_proxy.api.websockets.connect", AsyncMock()) as connect:
            await handler(downstream, "responses")

        self.assertEqual(downstream.close_code, 1008)
        connect.assert_not_awaited()

    async def test_codex_response_frames_are_forwarded_and_record_ttft(self):
        config = _config()
        pool = KeyPool([])
        pool.entries = [KeyEntry(
            "upstream-secret", "online", group_id="group-1",
            routing_capabilities={"platform": "openai", "endpoint_families": ["responses"]},
        )]
        pool.finalize_entries()
        request = json.dumps({
            "type": "response.create", "model": "gpt-5", "input": [],
        })
        metadata = json.dumps({"type": "response.created", "response": {"id": "resp-1"}})
        token = json.dumps({"type": "response.output_text.delta", "delta": "hello"})
        completed = json.dumps({"type": "response.completed", "response": {"id": "resp-1"}})
        downstream = FakeDownstream(
            [{"type": "websocket.receive", "text": request}],
            {"authorization": "Bearer pool-access", "openai-beta": "responses_websockets=2026-02-06"},
        )
        upstream = FakeUpstream([metadata, token, completed])
        store = SimpleNamespace(write=AsyncMock())
        handler = create_websocket_handler(store)

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test/v1": pool}), \
                patch("retry_proxy.api.match_route", return_value=(
                    "https://upstream.test/v1", "test", "responses",
                )), \
                patch("retry_proxy.api.websockets.connect", AsyncMock(return_value=upstream)) as connect:
            await handler(downstream, "responses")

        self.assertEqual(upstream.sent, [request])
        self.assertEqual(downstream.sent, [metadata, token, completed])
        self.assertTrue(upstream.closed)
        self.assertEqual(pool.entries[0].ttft_samples, 1)
        connect_headers = connect.await_args.kwargs["additional_headers"]
        self.assertEqual(connect_headers["authorization"], "Bearer upstream-secret")
        self.assertEqual(connect_headers["openai-beta"], "responses_websockets=2026-02-06")
        self.assertNotIn("pool-access", str(connect_headers))
        store.write.assert_awaited_once()

    async def test_warmup_response_does_not_record_ttft(self):
        config = _config()
        pool = KeyPool([("upstream-secret", "online")])
        request = json.dumps({
            "type": "response.create", "model": "gpt-5", "input": [], "generate": False,
        })
        downstream = FakeDownstream(
            [{"type": "websocket.receive", "text": request}],
            {"authorization": "Bearer pool-access"},
        )
        upstream = FakeUpstream([
            json.dumps({"type": "response.completed", "response": {"id": "resp-warm"}}),
        ])
        handler = create_websocket_handler(SimpleNamespace(write=AsyncMock()))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test/v1": pool}), \
                patch("retry_proxy.api.match_route", return_value=(
                    "https://upstream.test/v1", "test", "responses",
                )), \
                patch("retry_proxy.api.websockets.connect", AsyncMock(return_value=upstream)):
            await handler(downstream, "responses")

        self.assertEqual(pool.entries[0].ttft_samples, 0)

    async def test_later_incompatible_model_is_not_forwarded(self):
        config = _config()
        pool = KeyPool([])
        pool.entries = [KeyEntry(
            "upstream-secret", "text", models=("gpt-text-*",),
            routing_capabilities={"platform": "openai", "endpoint_families": ["responses"]},
        )]
        pool.finalize_entries()
        first = json.dumps({"type": "response.create", "model": "gpt-text-1", "input": []})
        second = json.dumps({"type": "response.create", "model": "gpt-image-1", "input": []})
        downstream = FakeDownstream([
            {"type": "websocket.receive", "text": first},
            {"type": "websocket.receive", "text": second},
        ], {"authorization": "Bearer pool-access"})
        upstream = FakeUpstream([])
        handler = create_websocket_handler(SimpleNamespace(write=AsyncMock()))

        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test/v1": pool}), \
                patch("retry_proxy.api.match_route", return_value=(
                    "https://upstream.test/v1", "test", "responses",
                )), \
                patch("retry_proxy.api.websockets.connect", AsyncMock(return_value=upstream)):
            await handler(downstream, "responses")

        self.assertEqual(upstream.sent, [first])
        self.assertEqual(downstream.close_code, 1008)


if __name__ == "__main__":
    unittest.main()
