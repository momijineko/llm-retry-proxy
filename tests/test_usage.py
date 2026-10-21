import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Request

from retry_proxy.usage import UsageAccumulator


def _sse_frame(payload):
    data = json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode("utf-8")


class UsageAccumulatorTests(unittest.TestCase):
    def test_openai_chat_non_stream_usage(self):
        acc = UsageAccumulator("chat", is_sse=False)
        body = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
        }
        acc.feed_chunk(json.dumps(body).encode("utf-8"))
        self.assertEqual(acc.finalize(), (12, 34, 46, 0))

    def test_openai_chat_stream_usage_on_final_frame(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"choices": [{"delta": {"content": "hi"}}]}))
        acc.feed_chunk(_sse_frame({
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
        }))
        acc.feed_chunk(b"data: [DONE]\n\n")
        self.assertEqual(acc.finalize(), (7, 9, 16, 0))

    def test_openai_chat_stream_without_usage_returns_none(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"choices": [{"delta": {"content": "hi"}}]}))
        acc.feed_chunk(b"data: [DONE]\n\n")
        self.assertIsNone(acc.finalize())

    def test_responses_stream_usage(self):
        acc = UsageAccumulator("responses", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"type": "response.output_text.delta", "delta": "hi"}))
        acc.feed_chunk(_sse_frame({
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 5, "output_tokens": 11, "total_tokens": 16}},
        }))
        self.assertEqual(acc.finalize(), (5, 11, 16, 0))

    def test_responses_non_stream_usage(self):
        acc = UsageAccumulator("responses", is_sse=False)
        acc.feed_chunk(json.dumps({
            "id": "resp_1",
            "usage": {
                "input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                "input_tokens_details": {"cached_tokens": 5},
            },
        }).encode("utf-8"))
        self.assertEqual(acc.finalize(), (11, 7, 18, 5))

    def test_gemini_stream_usage_takes_final_frame(self):
        acc = UsageAccumulator("gemini", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "candidates": [{"content": {"parts": [{"text": "he"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
        }))
        acc.feed_chunk(_sse_frame({
            "candidates": [{"content": {"parts": [{"text": "llo"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5, "totalTokenCount": 8},
        }))
        self.assertEqual(acc.finalize(), (3, 5, 8, 0))

    def test_gemini_derives_completion_when_candidates_missing(self):
        acc = UsageAccumulator("gemini", is_sse=False)
        acc.feed_chunk(json.dumps({
            "usageMetadata": {"promptTokenCount": 11, "totalTokenCount": 18},
        }).encode("utf-8"))
        self.assertEqual(acc.finalize(), (11, 7, 18, 0))

    def test_anthropic_messages_usage(self):
        acc = UsageAccumulator("messages", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 20, "output_tokens": 1}},
        }))
        acc.feed_chunk(_sse_frame({
            "type": "message_delta",
            "usage": {"output_tokens": 15},
        }))
        # total should be derived from prompt + completion when upstream omits it
        self.assertEqual(acc.finalize(), (20, 15, 35, 0))

    def test_anthropic_non_stream_sums_cache_input_categories(self):
        acc = UsageAccumulator("messages", is_sse=False)
        acc.feed_chunk(json.dumps({
            "type": "message",
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 75,
                "output_tokens": 10,
            },
        }).encode("utf-8"))
        self.assertEqual(acc.finalize(), (100, 10, 110, 75))

    def test_unsupported_family_returns_none(self):
        acc = UsageAccumulator("images", is_sse=False)
        acc.feed_chunk(b'{"usage": {"total_tokens": 1}}')
        self.assertIsNone(acc.finalize())

    def test_split_sse_frame_across_chunks(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        frame = _sse_frame({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}})
        mid = len(frame) // 2
        acc.feed_chunk(frame[:mid])
        acc.feed_chunk(frame[mid:])
        self.assertEqual(acc.finalize(), (2, 4, 6, 0))

    def test_split_crlf_does_not_end_multiline_sse_frame_early(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(b'data: {"usage":{"prompt_tokens":3,\r')
        acc.feed_chunk(
            b'\ndata: "completion_tokens":5,"total_tokens":8}}\r\n\r\n'
        )
        self.assertEqual(acc.finalize(), (3, 5, 8, 0))

    def test_bare_cr_sse_delimiter_is_flushed_on_finalize(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(
            b'data: {"usage":{"prompt_tokens":3,'
            b'"completion_tokens":5,"total_tokens":8}}\r\r'
        )
        self.assertEqual(acc.finalize(), (3, 5, 8, 0))

    def test_non_stream_invalid_json_returns_none(self):
        acc = UsageAccumulator("chat", is_sse=False)
        acc.feed_chunk(b"not json at all")
        self.assertIsNone(acc.finalize())

    def test_non_stream_large_body_returns_none_when_buffer_limit_exceeded(self):
        # A non-stream body larger than the buffer cap is discarded because a
        # partial JSON object cannot be decoded reliably.
        acc = UsageAccumulator("chat", is_sse=False)
        acc._max_buffer = 64
        prefix = b'{"choices":[{"message":{"content":"' + b"x" * 200 + b'"}}],'
        suffix = b'"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}'
        acc.feed_chunk(prefix + suffix)
        self.assertIsNone(acc.finalize())

    def test_non_stream_usage_in_tail_when_body_under_cap(self):
        # The normal happy path: full body fits and usage at the tail parses.
        acc = UsageAccumulator("chat", is_sse=False)
        body = '{"choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":4,"completion_tokens":6,"total_tokens":10}}'
        acc.feed_chunk(body.encode("utf-8"))
        self.assertEqual(acc.finalize(), (4, 6, 10, 0))

    def test_openai_chat_cached_tokens(self):
        # OpenAI Chat nests cache-read tokens under prompt_tokens_details.
        acc = UsageAccumulator("chat", is_sse=False)
        body = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 9}},
        }
        acc.feed_chunk(json.dumps(body).encode("utf-8"))
        self.assertEqual(acc.finalize(), (12, 8, 20, 9))

    def test_openai_embeddings_cached_tokens(self):
        acc = UsageAccumulator("embeddings", is_sse=False)
        body = {
            "data": [{"embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 50, "total_tokens": 50,
                      "prompt_tokens_details": {"cached_tokens": 50}},
        }
        acc.feed_chunk(json.dumps(body).encode("utf-8"))
        self.assertEqual(acc.finalize(), (50, 0, 50, 50))

    def test_responses_cached_tokens(self):
        # OpenAI Responses API nests cache-read tokens under input_tokens_details.
        acc = UsageAccumulator("responses", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"type": "response.output_text.delta", "delta": "hi"}))
        acc.feed_chunk(_sse_frame({
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 15, "output_tokens": 10, "total_tokens": 25,
                                   "input_tokens_details": {"cached_tokens": 15}}},
        }))
        self.assertEqual(acc.finalize(), (15, 10, 25, 15))

    def test_gemini_cached_tokens(self):
        # Gemini exposes cachedContentTokenCount as a flat field on usageMetadata.
        acc = UsageAccumulator("gemini", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20,
                              "totalTokenCount": 120, "cachedContentTokenCount": 80},
        }))
        self.assertEqual(acc.finalize(), (100, 20, 120, 80))

    def test_anthropic_cached_tokens(self):
        # Anthropic exposes cache_read_input_tokens as a flat field on message_start.usage.
        acc = UsageAccumulator("messages", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 30, "output_tokens": 1,
                                  "cache_read_input_tokens": 25}},
        }))
        acc.feed_chunk(_sse_frame({
            "type": "message_delta",
            "usage": {"output_tokens": 10},
        }))
        self.assertEqual(acc.finalize(), (55, 10, 65, 25))

    def test_no_cached_tokens_defaults_zero(self):
        # When the upstream response omits any cache field, cached stays 0 but
        # usage is still parsed (cached is the fourth element of the tuple).
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }))
        self.assertEqual(acc.finalize(), (5, 5, 10, 0))


class UpstreamModelTests(unittest.TestCase):
    """The model name the upstream declares it served, collected per family."""

    def test_chat_non_stream_records_declared_model(self):
        acc = UsageAccumulator("chat", is_sse=False)
        acc.feed_chunk(json.dumps({
            "model": "deepseek/deepseek-flash",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }).encode("utf-8"))
        acc.finalize()
        self.assertEqual(acc.upstream_model, "deepseek/deepseek-flash")

    def test_chat_stream_takes_first_declared_model(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"model": "opencode/deepseek-v4.1-flash",
                                   "choices": [{"delta": {"content": "hi"}}]}))
        acc.feed_chunk(_sse_frame({"model": "opencode/deepseek-v4.1-flash",
                                   "choices": [],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                             "total_tokens": 3}}))
        self.assertEqual(acc.finalize(), (1, 2, 3, 0))
        self.assertEqual(acc.upstream_model, "opencode/deepseek-v4.1-flash")

    def test_responses_stream_records_nested_response_model(self):
        acc = UsageAccumulator("responses", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "type": "response.completed",
            "response": {"model": "deepseek/deepseek-v4.1-flash",
                         "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11}},
        }))
        self.assertEqual(acc.finalize(), (5, 6, 11, 0))
        self.assertEqual(acc.upstream_model, "deepseek/deepseek-v4.1-flash")

    def test_messages_stream_records_model_from_message_start(self):
        acc = UsageAccumulator("messages", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "type": "message_start",
            "message": {"model": "anthropic/claude-sonnet-4-20250514",
                        "usage": {"input_tokens": 10, "output_tokens": 1}},
        }))
        acc.feed_chunk(_sse_frame({"type": "message_delta", "usage": {"output_tokens": 4}}))
        self.assertEqual(acc.finalize(), (10, 4, 14, 0))
        self.assertEqual(acc.upstream_model, "anthropic/claude-sonnet-4-20250514")

    def test_gemini_records_model_version(self):
        acc = UsageAccumulator("gemini", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({
            "modelVersion": "gemini-2.5-flash-001",
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2,
                              "totalTokenCount": 5},
        }))
        self.assertEqual(acc.finalize(), (3, 2, 5, 0))
        self.assertEqual(acc.upstream_model, "gemini-2.5-flash-001")

    def test_declared_model_is_collected_without_usage(self):
        # A stream that never reports usage must still expose the declaration.
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"model": "deepseek/flash",
                                   "choices": [{"delta": {"content": "hi"}}]}))
        acc.feed_chunk(b"data: [DONE]\n\n")
        self.assertIsNone(acc.finalize())
        self.assertEqual(acc.upstream_model, "deepseek/flash")

    def test_unsupported_family_ignores_declared_model(self):
        acc = UsageAccumulator("images", is_sse=False)
        acc.feed_chunk(b'{"model":"flux/dev"}')
        self.assertIsNone(acc.finalize())
        self.assertEqual(acc.upstream_model, "")

    def test_blank_or_non_string_declarations_are_ignored(self):
        acc = UsageAccumulator("chat", is_sse=True, content_type="text/event-stream")
        acc.feed_chunk(_sse_frame({"model": "   ", "choices": []}))
        acc.feed_chunk(_sse_frame({"model": 7, "choices": []}))
        acc.feed_chunk(_sse_frame({"model": "deepseek/flash", "choices": []}))
        acc.finalize()
        self.assertEqual(acc.upstream_model, "deepseek/flash")


class TokenAggregationTests(unittest.TestCase):
    def test_agg_by_accumulates_tokens(self):
        from retry_proxy.stats import _agg_by
        records = [
            {"model": "gpt-4", "retries": 0, "final_status": 200, "succeeded": True,
             "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
             "cached_tokens": 8},
            {"model": "gpt-4", "retries": 1, "final_status": 200, "succeeded": True,
             "prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
             "cached_tokens": 3},
        ]
        row = _agg_by(records, "model", "model")[0]
        self.assertEqual(row["prompt_tokens"], 15)
        self.assertEqual(row["completion_tokens"], 27)
        self.assertEqual(row["total_tokens"], 42)
        self.assertEqual(row["cached_tokens"], 11)

    def test_agg_by_handles_missing_token_fields(self):
        from retry_proxy.stats import _agg_by
        records = [
            {"model": "gpt-4", "retries": 0, "final_status": 200, "succeeded": True},
            {"model": "gpt-4", "retries": 0, "final_status": 200, "succeeded": True,
             "total_tokens": 8},
        ]
        row = _agg_by(records, "model", "model")[0]
        self.assertEqual(row["total_tokens"], 8)
        self.assertEqual(row["prompt_tokens"], 0)


class LogStoreTokenSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import os
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            log_dir=self.tempdir.name,
            log_retention_days=0,
            summary_file=os.path.join(self.tempdir.name, "_summary.json"),
            legacy_log_file=os.path.join(self.tempdir.name, "legacy.json"),
        )
        self._patcher = patch("retry_proxy.log_store.settings", self.config)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tempdir.cleanup()

    async def test_summary_accumulates_tokens_and_persists(self):
        from retry_proxy.log_store import RetryLogStore
        store = RetryLogStore()
        store.initialize()
        await store.write({
            "ts": "2026-07-25T10:00:00.000", "model": "gpt-4", "provider": "p",
            "path": "/v1/chat/completions", "method": "POST",
            "final_status": 200, "succeeded": True, "retries": 0,
            "upstream_status": 200, "duration_s": 0.5,
            "prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300,
            "cached_tokens": 80,
        })
        self.assertEqual(store.summary["total_tokens"], 300)
        self.assertEqual(store.summary["total_prompt_tokens"], 100)
        self.assertEqual(store.summary["total_cached_tokens"], 80)
        self.assertEqual(store.summary["by_model"]["p/gpt-4"]["total_tokens"], 300)
        self.assertEqual(store.summary["by_model"]["p/gpt-4"]["cached_tokens"], 80)

        # Reload from disk to verify persistence
        restored = RetryLogStore()
        restored.initialize()
        self.assertEqual(restored.summary["total_tokens"], 300)
        self.assertEqual(restored.summary["total_cached_tokens"], 80)
        self.assertEqual(restored.summary["version"], 8)

    async def test_legacy_summary_without_tokens_is_backfilled(self):
        import json
        from retry_proxy.log_store import RetryLogStore
        # A v6 summary written by the cancelled-statistics feature (main) has
        # cancelled/first_ok fields but predates token tracking — it must be
        # backfilled in place (no rebuild, since version is already 6) with
        # zero token defaults while preserving accumulated request history.
        legacy = {
            "version": 6, "total_requests": 5, "total_retries": 0,
            "total_succeeded": 5, "total_failed": 0, "total_cancelled": 0,
            "total_first_ok": 5,
            "by_provider": {"p": {"requests": 5, "retries": 0, "succeeded": 5,
                                  "first_ok": 5, "failed": 0, "cancelled": 0,
                                  "max_retries": 0}},
            "by_model": {}, "by_key": {}, "by_status": {},
            "first_ts": None, "last_ts": None,
        }
        with open(self.config.summary_file, "w") as f:
            json.dump(legacy, f)
        store = RetryLogStore()
        store.initialize()
        # v6 is upgraded in place; non-token history is preserved and token
        # fields are backfilled with zero defaults.
        self.assertEqual(store.summary["version"], 8)
        self.assertEqual(store.summary["total_requests"], 5)
        self.assertEqual(store.summary["total_cancelled"], 0)
        self.assertEqual(store.summary["total_tokens"], 0)
        self.assertEqual(store.summary["total_prompt_tokens"], 0)
        self.assertEqual(store.summary["by_provider"]["p"]["total_tokens"], 0)
        self.assertEqual(store.summary["by_provider"]["p"]["requests"], 5)
        self.assertEqual(store.summary["by_provider"]["p"]["cancelled"], 0)


class StreamOptionsInjectionTests(unittest.TestCase):
    def _inject(self, body, family="chat", enabled=True):
        from types import SimpleNamespace
        from unittest.mock import patch
        from retry_proxy.api import _maybe_inject_stream_usage
        cfg = SimpleNamespace(token_stats_inject_usage=enabled)
        with patch("retry_proxy.api.settings", cfg):
            return _maybe_inject_stream_usage(body, family)

    def test_injects_include_usage_for_streaming_chat(self):
        body = json.dumps({"model": "gpt-4", "stream": True}).encode("utf-8")
        out = self._inject(body)
        payload = json.loads(out)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_skips_when_already_has_include_usage(self):
        body = json.dumps({"stream": True, "stream_options": {"include_usage": True}}).encode("utf-8")
        out = self._inject(body)
        self.assertEqual(out, body)

    def test_respects_explicit_include_usage_false(self):
        # An explicit opt-out is respected; the proxy does not force usage on.
        body = json.dumps({"stream": True, "stream_options": {"include_usage": False}}).encode("utf-8")
        out = self._inject(body)
        self.assertEqual(out, body)

    def test_preserves_sibling_stream_options_keys(self):
        body = json.dumps({"stream": True, "stream_options": {"temperature": 0.5}}).encode("utf-8")
        out = self._inject(body)
        opts = json.loads(out)["stream_options"]
        self.assertEqual(opts, {"temperature": 0.5, "include_usage": True})

    def test_skips_non_chat_family(self):
        body = json.dumps({"stream": True}).encode("utf-8")
        out = self._inject(body, family="responses")
        self.assertEqual(out, body)

    def test_skips_non_streaming_request(self):
        body = json.dumps({"model": "gpt-4", "stream": False}).encode("utf-8")
        out = self._inject(body)
        self.assertEqual(out, body)

    def test_disabled_config_returns_body_unchanged(self):
        body = json.dumps({"model": "gpt-4", "stream": True}).encode("utf-8")
        out = self._inject(body, enabled=False)
        self.assertEqual(out, body)


class BodyGenLogWriteTimingTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the unified finally-write path in body_gen.

    Before the token-stats change, non-responses streams wrote their log record
    *before* streaming started, so a mid-stream transport error left the record
    marked as successful and no usage could be attached. These tests pin the
    new behavior: the record is written once, after the stream finishes, with
    usage and stream-outcome fields.
    """

    def _build_proxy(self, upstream_response, store, family="chat", path="/v1/chat/completions",
                     request_body=b'{"model":"gpt-4","stream":true}'):
        from retry_proxy.api import create_handlers
        from retry_proxy.key_pool import KeyPool
        pool = KeyPool([("pool-key", "pool-key")])
        entry = pool.entries[0]
        result = SimpleNamespace(
            response=upstream_response, winner_attempt=1, total_sent=1,
            last_status=upstream_response.status_code, retry_codes=[], first_ok=True,
            key_id=entry.key_id,
            key_attempts=[{"key_id": entry.key_id, "available": True}],
            started_at=time.time(), key_entry=entry,
            response_started_mono=time.monotonic(),
        )
        service = SimpleNamespace(
            request=lambda *args, **kwargs: None,
            hedge_mode_for=lambda request_pool: "off",
        )
        config = SimpleNamespace(
            proxy_api_key="", dlp_mode="off", dlp_max_body_bytes=1024,
            image_upstream_user_agent="", image_upstream_originator="",
            token_stats_inject_usage=False,
        )
        proxy = create_handlers(service, store)[-1]
        request = Request({
            "type": "http", "method": "POST", "path": path,
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        }, receive=AsyncMock(return_value={
            "type": "http.request", "body": request_body, "more_body": False,
        }))
        return proxy, request, pool, entry, config, result

    async def _run(self, proxy, request, pool, config, result):
        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "v1/chat/completions")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)):
            response = await proxy("v1/chat/completions", request)
            body = b"".join([chunk async for chunk in response.body_iterator])
            return response, body

    async def test_chat_stream_writes_log_once_with_usage_in_finally(self):
        sse = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":12,"total_tokens":20}}\n\n'
            b'data: [DONE]\n\n'
        )
        upstream_response = httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/v1/chat/completions"),
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(upstream_response, store)
        _, body = await self._run(proxy, request, pool, config, result)

        self.assertEqual(body, sse)
        store.write.assert_awaited_once()
        record = store.write.await_args.args[0]
        # The record is written after the stream completes (succeeded=True),
        # and the usage extracted from the final SSE frame is attached.
        self.assertTrue(record["succeeded"])
        self.assertEqual(record["prompt_tokens"], 8)
        self.assertEqual(record["completion_tokens"], 12)
        self.assertEqual(record["total_tokens"], 20)
        self.assertEqual(record["cached_tokens"], 0)

    async def test_mixed_case_sse_content_type_is_detected(self):
        sse = (
            b'data: {"choices":[],"usage":{"prompt_tokens":3,'
            b'"completion_tokens":5,"total_tokens":8}}\n\n'
            b'data: [DONE]\n\n'
        )
        upstream_response = httpx.Response(
            200, content=sse, headers={"content-type": "Text/Event-Stream"},
            request=httpx.Request("POST", "https://upstream.test/v1/chat/completions"),
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(
            upstream_response, store,
        )

        response, body = await self._run(proxy, request, pool, config, result)

        self.assertEqual(body, sse)
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        record = store.write.await_args.args[0]
        self.assertEqual(record["prompt_tokens"], 3)
        self.assertEqual(record["completion_tokens"], 5)
        self.assertEqual(record["total_tokens"], 8)

    async def test_chat_stream_transport_error_marks_failed_in_finally(self):
        # The upstream stream raises a transport error mid-way. The early-write
        # path previously recorded this as successful; now the finally path
        # reflects the failure.
        class _TransportStream:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                raise httpx.TransportError("connection reset")

            async def aclose(self):
                pass

        upstream_response = _TransportStream()
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(upstream_response, store)
        await self._run(proxy, request, pool, config, result)

        store.write.assert_awaited_once()
        record = store.write.await_args.args[0]
        self.assertFalse(record["succeeded"])
        self.assertEqual(record["stream_status"], "transport_error")

    async def test_chat_stream_client_cancel_is_neutral(self):
        started = asyncio.Event()
        pending = asyncio.Event()

        class _SlowStream:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                started.set()
                await pending.wait()

            async def aclose(self):
                pass

        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(_SlowStream(), store)
        with patch("retry_proxy.api.settings", config), \
                patch("retry_proxy.config.settings", config), \
                patch("retry_proxy.api.KEY_POOLS", {"https://upstream.test": pool}), \
                patch("retry_proxy.api.match_route",
                      return_value=("https://upstream.test", "test", "v1/chat/completions")), \
                patch("retry_proxy.api._run_until_disconnect", AsyncMock(return_value=result)):
            response = await proxy("v1/chat/completions", request)

            async def consume():
                async for _chunk in response.body_iterator:
                    pass

            task = asyncio.create_task(consume())
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        store.write.assert_awaited_once()
        record = store.write.await_args.args[0]
        self.assertFalse(record["succeeded"])
        self.assertEqual(record["stream_status"], "cancelled")
        from retry_proxy.stats import compute_stats
        summary = compute_stats([record], "today", {})["summary"]
        self.assertEqual(summary["failed_requests"], 0)
        self.assertEqual(summary["cancelled_requests"], 1)

    async def test_non_stream_chat_attaches_usage_from_buffered_body(self):
        body_json = (
            b'{"choices":[{"message":{"content":"hi"}}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":5,"total_tokens":8}}'
        )
        upstream_response = httpx.Response(
            200, content=body_json, headers={"content-type": "application/json"},
            request=httpx.Request("POST", "https://upstream.test/v1/chat/completions"),
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(
            upstream_response, store,
            request_body=b'{"model":"gpt-4","stream":false}',
        )
        _, body = await self._run(proxy, request, pool, config, result)

        self.assertEqual(body, body_json)
        store.write.assert_awaited_once()
        record = store.write.await_args.args[0]
        self.assertTrue(record["succeeded"])
        self.assertEqual(record["total_tokens"], 8)
        self.assertEqual(record["prompt_tokens"], 3)

    async def test_chat_stream_attaches_cached_tokens_from_final_frame(self):
        # End-to-end: cache-read tokens from the final SSE frame's
        # prompt_tokens_details propagate through usage_extra to the log record.
        sse = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":10,"total_tokens":60,'
            b'"prompt_tokens_details":{"cached_tokens":40}}}\n\n'
            b'data: [DONE]\n\n'
        )
        upstream_response = httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/v1/chat/completions"),
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(upstream_response, store)
        _, body = await self._run(proxy, request, pool, config, result)

        self.assertEqual(body, sse)
        record = store.write.await_args.args[0]
        self.assertTrue(record["succeeded"])
        self.assertEqual(record["prompt_tokens"], 50)
        self.assertEqual(record["total_tokens"], 60)
        self.assertEqual(record["cached_tokens"], 40)

    async def test_chat_stream_logs_upstream_declared_model(self):
        # The upstream declares a provider-path model name. It is recorded for
        # diagnostics while the response body is passed through untouched.
        sse = (
            b'data: {"model":"deepseek/deepseek-v4.1-flash","choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"model":"deepseek/deepseek-v4.1-flash","choices":[],"usage":'
            b'{"prompt_tokens":5,"completion_tokens":6,"total_tokens":11}}\n\n'
            b'data: [DONE]\n\n'
        )
        upstream_response = httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/v1/chat/completions"),
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(
            upstream_response, store,
            request_body=b'{"model":"deepseek-v4.1-flash","stream":true}',
        )
        _, body = await self._run(proxy, request, pool, config, result)

        self.assertEqual(body, sse)
        record = store.write.await_args.args[0]
        self.assertEqual(record["model"], "deepseek-v4.1-flash")
        self.assertEqual(record["upstream_model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(record["total_tokens"], 11)

    async def test_chat_stream_omits_upstream_model_when_undeclared(self):
        sse = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            b'data: [DONE]\n\n'
        )
        upstream_response = httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"},
            request=httpx.Request("POST", "https://upstream.test/v1/chat/completions"),
        )
        store = SimpleNamespace(write=AsyncMock())
        proxy, request, pool, entry, config, result = self._build_proxy(upstream_response, store)
        await self._run(proxy, request, pool, config, result)

        record = store.write.await_args.args[0]
        self.assertNotIn("upstream_model", record)
        self.assertEqual(record["total_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
