import unittest
from types import SimpleNamespace

from retry_proxy.sse2ws import (
    ResponsesWSBridge,
    _SSEParser,
    _build_error_event,
    _dedup_context,
    _embedded_failure_status,
    _extract_input_items,
    _items_have_prefix,
    _merge_replay_items,
    is_responses_ws_path,
)


class IsResponsesWsPathTests(unittest.TestCase):
    def test_recognizes_responses_endpoints(self):
        for path in ("v1/responses", "/v1/responses", "responses", "/responses/",
                     "aihub/v1/responses"):
            self.assertTrue(is_responses_ws_path(path), path)

    def test_rejects_other_endpoints(self):
        for path in ("v1/chat/completions", "v1/messages", "", "images/generations"):
            self.assertFalse(is_responses_ws_path(path), path)


class SSEParserTests(unittest.TestCase):
    def test_splits_multiple_events(self):
        parser = _SSEParser()
        parser.feed(b'data: {"type":"response.created"}\n\n'
                    b'data: {"type":"response.completed"}\n\n')
        events = list(parser.events())
        self.assertEqual([e[0]["type"] for e in events],
                         ["response.created", "response.completed"])

    def test_handles_crlf_and_partial_chunks(self):
        parser = _SSEParser()
        parser.feed(b'event: response.created\r\ndata: {"type":"response.created"}\r\n\r\nda')
        parser.feed(b'ta: {"type":"response.in_progress"}\n\n')
        events = list(parser.events())
        self.assertEqual([e[0]["type"] for e in events],
                         ["response.created", "response.in_progress"])
        self.assertEqual(events[0][1], '{"type":"response.created"}')

    def test_ignores_done_sentinel(self):
        parser = _SSEParser()
        parser.feed(b'data: [DONE]\n\n')
        self.assertEqual(list(parser.events()), [])

    def test_ignores_comment_only_frames(self):
        parser = _SSEParser()
        parser.feed(b': keepalive\n\n')
        self.assertEqual(list(parser.events()), [])

    def test_ignores_invalid_json(self):
        parser = _SSEParser()
        parser.feed(b'data: not-json\n\n')
        self.assertEqual(list(parser.events()), [])


class ReplayContextTests(unittest.TestCase):
    def make_bridge(self):
        ws = SimpleNamespace(
            headers={}, scope={"path": "/v1/responses", "query_string": b""},
            client=("127.0.0.1", 1234),
        )
        return ResponsesWSBridge(ws, "v1/responses", None, None)

    def test_first_turn_payload_normalized(self):
        bridge = self.make_bridge()
        payload = {
            "type": "response.create", "model": "gpt-5.6",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "store": False, "stream": False,
        }
        body = bridge._prepare_turn_payload(payload)
        self.assertEqual(body["stream"], True)
        self.assertNotIn("type", body)
        self.assertNotIn("generate", body)
        self.assertNotIn("previous_response_id", body)
        self.assertEqual(len(bridge._pending_replay), 1)

    def test_continuation_replays_full_context(self):
        bridge = self.make_bridge()
        user_msg = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        tool_call = {"type": "function_call", "id": "call_1", "name": "ls",
                     "arguments": "{}", "call_id": "call_1"}
        tool_output = {"type": "function_call_output", "call_id": "call_1", "output": "file.txt"}
        # 重放上下文里的 function_call 被归一化成 input 形态（去掉 id/status）。
        replay_call = {"type": "function_call", "call_id": "call_1",
                       "name": "ls", "arguments": "{}"}

        bridge._prepare_turn_payload({
            "type": "response.create", "model": "m", "input": [user_msg],
        })
        bridge._handle_event({"type": "response.output_item.done", "item": tool_call})
        bridge._commit_replay()
        self.assertEqual(bridge._replay_input, [user_msg, replay_call])

        payload = {
            "type": "response.create", "model": "m",
            "input": [tool_output], "previous_response_id": "resp_1",
        }
        body = bridge._prepare_turn_payload(payload)
        self.assertNotIn("previous_response_id", body)
        self.assertEqual(body["input"], [user_msg, replay_call, tool_output])

    def test_duplicate_tool_call_collected_once(self):
        # 同一工具调用会同时出现在 output_item.done 与 completed.output
        # （一个有 id/status、一个没有），按 call_id 去重后只能保留一次。
        bridge = self.make_bridge()
        user_msg = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        call_with_id = {"type": "function_call", "id": "fc_1", "status": "completed",
                        "call_id": "call_1", "name": "ls", "arguments": "{}"}
        call_minimal = {"type": "function_call", "call_id": "call_1",
                        "name": "ls", "arguments": "{}"}

        bridge._prepare_turn_payload({
            "type": "response.create", "model": "m", "input": [user_msg],
        })
        bridge._handle_event({"type": "response.output_item.done", "item": call_with_id})
        bridge._handle_event({"type": "response.completed",
                              "response": {"output": [call_minimal]}})
        bridge._commit_replay()
        self.assertEqual(
            bridge._replay_input,
            [user_msg, {"type": "function_call", "call_id": "call_1",
                        "name": "ls", "arguments": "{}"}],
        )

    def test_redundant_full_context_not_duplicated(self):
        bridge = self.make_bridge()
        user_msg = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
        bridge._prepare_turn_payload({
            "type": "response.create", "model": "m", "input": [user_msg],
        })
        bridge._commit_replay()
        payload = {
            "type": "response.create", "model": "m",
            "input": [user_msg], "previous_response_id": "resp_1",
        }
        body = bridge._prepare_turn_payload(payload)
        self.assertEqual(body["input"], [user_msg])

    def test_full_history_with_output_metadata_not_duplicated(self):
        bridge = self.make_bridge()
        user = {"role": "user", "content": "hi"}
        call = {"type": "function_call", "call_id": "call_1",
                "name": "ls", "arguments": "{}"}
        result = {"type": "function_call_output", "call_id": "call_1",
                  "output": "file.txt"}
        bridge._replay_input = [user, call]
        body = bridge._prepare_turn_payload({
            "type": "response.create", "previous_response_id": "resp_1",
            "input": [user, dict(call, id="fc_1", status="completed"), result],
        })
        self.assertEqual(body["input"], [user, call, result])

    def test_partial_replay_prefers_current_call_and_retains_result(self):
        bridge = self.make_bridge()
        user = {"role": "user", "content": "hi"}
        call = {"type": "function_call", "call_id": "call_1",
                "name": "ls", "arguments": "{}"}
        updated = dict(call, arguments='{"path":"."}')
        result = {"type": "function_call_output", "call_id": "call_1",
                  "output": "file.txt"}
        bridge._replay_input = [user, call]
        body = bridge._prepare_turn_payload({
            "type": "response.create", "input": [updated, result],
        })
        self.assertEqual(body["input"], [user, updated, result])
        self.assertEqual(bridge._replay_input, [user, call])
        bridge._collected = [dict(updated, id="fc_1")]
        bridge._commit_replay()
        self.assertEqual(bridge._replay_input, [user, updated, result])

    def test_merge_keeps_call_and_output_for_each_tool_type(self):
        for kind, output_kind in (
            ("function_call", "function_call_output"),
            ("custom_tool_call", "custom_tool_call_output"),
            ("tool_search_call", "tool_search_output"),
            ("mcp_tool_call", "mcp_tool_call_output"),
        ):
            with self.subTest(kind=kind):
                call = {"type": kind, "call_id": "call_1"}
                result = {"type": output_kind, "call_id": "call_1",
                          "output": "old"}
                updated = dict(result, output="new")
                self.assertEqual(_merge_replay_items([call, result], [updated]),
                                 [call, updated])

    def test_merge_preserves_messages_and_distinct_calls(self):
        message = {"role": "user", "content": "continue"}
        call = {"type": "function_call", "call_id": "call_1",
                "name": "ls", "arguments": "{}"}
        other = dict(call, call_id="call_2")
        self.assertEqual(_merge_replay_items([message, call], [message, other]),
                         [message, call, message, other])
        self.assertEqual(_merge_replay_items([message, call], []), [message, call])

    def test_merge_does_not_hide_conflicts_inside_client_input(self):
        call = {"type": "function_call", "call_id": "call_1"}
        conflict = dict(call, name="other")
        self.assertEqual(_merge_replay_items([], [call, conflict]),
                         [call, conflict])


class TerminalOutcomeTests(unittest.TestCase):
    def make_bridge(self):
        ws = SimpleNamespace(
            headers={}, scope={"path": "/v1/responses", "query_string": b""},
            client=("127.0.0.1", 1234),
        )
        return ResponsesWSBridge(ws, "v1/responses", None, None)

    def test_completed_is_success(self):
        bridge = self.make_bridge()
        bridge._handle_event({"type": "response.created"})
        bridge._handle_event({"type": "response.completed", "response": {"id": "r"}})
        outcome = bridge._finish_stream_outcome(200)
        self.assertTrue(outcome["succeeded"])
        self.assertEqual(outcome["status"], "response.completed")

    def test_incomplete_is_success(self):
        bridge = self.make_bridge()
        bridge._handle_event({"type": "response.incomplete"})
        outcome = bridge._finish_stream_outcome(200)
        self.assertTrue(outcome["succeeded"])
        self.assertEqual(outcome["status"], "response.incomplete")

    def test_failed_marks_failure_and_forwards_terminal(self):
        bridge = self.make_bridge()
        bridge._handle_event({
            "type": "response.failed",
            "response": {"error": {"status_code": 429, "message": "slow down"}},
        })
        outcome = bridge._finish_stream_outcome(200)
        self.assertFalse(outcome["succeeded"])
        self.assertTrue(outcome["forwarded_terminal"])

    def test_premature_eof_is_failure(self):
        bridge = self.make_bridge()
        bridge._handle_event({"type": "response.created"})
        outcome = bridge._finish_stream_outcome(200)
        self.assertFalse(outcome["succeeded"])
        self.assertEqual(outcome["status"], "eof")


class HelperTests(unittest.TestCase):
    def test_extract_input_items(self):
        self.assertEqual(_extract_input_items({"input": [1, 2]}), [1, 2])
        self.assertEqual(_extract_input_items({"input": {"type": "message"}}), [{"type": "message"}])
        self.assertEqual(_extract_input_items({"input": "hello"}), ["hello"])
        self.assertEqual(_extract_input_items({}), [])

    def test_items_have_prefix(self):
        self.assertTrue(_items_have_prefix([1, 2, 3], [1, 2]))
        self.assertFalse(_items_have_prefix([1, 3], [1, 2]))
        self.assertTrue(_items_have_prefix([], []))
        self.assertFalse(_items_have_prefix([1], [1, 2]))

    def test_build_error_event(self):
        event = _build_error_event(503, "  boom  ", "upstream_error")
        payload = __import__("json").loads(event)
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["status"], 503)
        self.assertEqual(payload["error"]["message"], "boom")

    def test_embedded_failure_status(self):
        self.assertEqual(_embedded_failure_status(
            {"type": "error", "error": {"status_code": 429}}), 429)
        self.assertEqual(_embedded_failure_status(
            {"type": "response.failed", "response": {"error": {"status": 403}}}), 403)
        self.assertIsNone(_embedded_failure_status({"type": "response.completed"}))

    def test_dedup_context(self):
        items = [
            {"type": "function_call", "id": "c1"},
            {"type": "function_call", "id": "c1"},
            {"type": "function_call", "id": "c2"},
        ]
        self.assertEqual(len(_dedup_context(items)), 2)


if __name__ == "__main__":
    unittest.main()
