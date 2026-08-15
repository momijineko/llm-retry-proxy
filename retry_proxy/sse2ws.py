"""Responses WebSocket <-> upstream HTTP/SSE 双向桥接（名称 sse2ws 指响应方向）。

方向标识（双向桥接，一次 WebSocket 连接内可连续多轮）：

- ``WS → SSE``（请求方向，客户端 → 上游）：客户端通过 WebSocket 发送
  ``response.create`` 文本帧，桥接转成一次上游 HTTP/SSE Responses 请求。
- ``SSE → WS``（响应方向，上游 → 客户端）：上游的 SSE 事件流被逐帧转成
  WebSocket JSON 文本帧推回客户端。

Codex CLI 等客户端通过 ``/v1/responses`` 建立 WebSocket 长连接，逐轮发送
``response.create`` 帧；本模块把每一轮翻译成一次上游 HTTP/SSE Responses 请求，
再把上游的 SSE 事件流逐帧转成 WebSocket JSON 文本帧推回客户端。

设计要点（参考 sub2api 的 openai_ws_http_bridge）：

- 无状态桥接：上游通常是纯 HTTP/SSE 端点，不理解 WS 连接级的
  ``previous_response_id`` 状态。因此连接内累积上下文 item，在续轮时把完整
  input 重放给上游并丢弃 ``previous_response_id``，保证多轮工具调用正确衔接。
- 首事件守护：上游返回了响应头但迟迟不发首帧时，按 ``SSE2WS_FIRST_EVENT_TIMEOUT``
  超时并重试整个 turn（``SSE2WS_FIRST_EVENT_RETRIES`` 次），避免挂死。
- 终止事件判定：以收到 ``response.completed`` / ``response.failed`` /
  ``response.incomplete`` / ``response.cancelled`` / ``error`` 为准，绝不把
  提前 EOF 当作成功。
"""

import asyncio
import json
import time
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from .access_control import resolve_client_ip
from .api import (
    _key_pool_secrets,
    classify_endpoint,
    classify_model_scope,
    outbound_request_headers,
    parse_request_session_id,
)
from .config import can_use_key_pool, logger, settings
from .dlp import inspect_json_body
from .key_pool import KEY_POOLS
from .retry import _mark_key_failure, _tag, reset_client_ip, set_client_ip
from .routes import build_proxy_url, match_route

SSE2WS_TERMINAL_EVENTS = frozenset({
    "response.completed", "response.failed", "response.incomplete",
    "response.cancelled", "response.done", "error",
})

SSE2WS_OK_TERMINAL_EVENTS = frozenset({
    "response.completed", "response.done", "response.incomplete",
})

TOOL_CALL_CONTEXT_TYPES = frozenset({
    "tool_call", "function_call", "local_shell_call", "tool_search_call",
    "custom_tool_call", "mcp_tool_call",
})
TOOL_CALL_OUTPUT_TYPES = frozenset({
    "function_call_output", "tool_search_output", "custom_tool_call_output",
    "mcp_tool_call_output",
})

SKIP_UPSTREAM_WS_HEADERS = (
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
)
# Codex WS 握手专属头，转发到 HTTP/SSE 上游没有意义，甚至可能让网关按 WS 协议
# 处理 HTTP 请求（如 openai-beta: responses_websockets=...、session/thread 粘性）。
SKIP_UPSTREAM_WS_SESSION_HEADERS = (
    "session-id", "thread-id", "x-client-request-id",
)


class _ClientDisconnected(Exception):
    pass


def is_responses_ws_path(path: str) -> bool:
    normalized = (path or "").strip("/").lower()
    return normalized == "responses" or normalized.endswith("/responses")


def _extract_input_items(payload) -> list:
    value = payload.get("input")
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        return [value]
    return []


def _canonical(item) -> str:
    try:
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(item)


def _items_have_prefix(items, prefix) -> bool:
    if not prefix:
        return True
    if len(items) < len(prefix):
        return False
    return all(_canonical(a) == _canonical(b) for a, b in zip(items, prefix))


def _is_context_item(item) -> bool:
    return isinstance(item, dict) and item.get("type") in TOOL_CALL_CONTEXT_TYPES


def _normalize_replay_item(item):
    """把输出形态的工具调用 item 归一化成上游 input 接受的形态。

    上游 output 里的 ``function_call`` 带 ``id`` / ``status`` 等输出字段，直接
    塞回 input 会导致部分上游（如 aihub）校验失败返回 502。input 形态只需要
    ``type`` / ``call_id`` / ``name`` / ``arguments``。
    """
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return item
    out = {}
    for key in ("type", "call_id", "name", "arguments"):
        if key in item:
            out[key] = item[key]
    return out


def _dedup_context(items) -> list:
    seen = set()
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # 去重 key 取自已归一化前的 item，保留 id/call_id 用于区分。
        key = item.get("call_id") or item.get("id") or _canonical(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(_normalize_replay_item(item))
    return out


def _build_error_event(status, message, error_type="server_error") -> str:
    message = (message or "").strip()
    if not message:
        message = "upstream request failed"
    return json.dumps({
        "type": "error",
        "status": status,
        "error": {"type": error_type, "message": message},
    }, ensure_ascii=False)


def _embedded_failure_status(payload):
    if not isinstance(payload, dict):
        return None
    candidates = []
    error = payload.get("error")
    if isinstance(error, dict):
        candidates.append(error.get("status_code"))
        candidates.append(error.get("status"))
        candidates.append(error.get("code"))
    response = payload.get("response")
    if isinstance(response, dict):
        nested = response.get("error")
        if isinstance(nested, dict):
            candidates.append(nested.get("status_code"))
            candidates.append(nested.get("status"))
            candidates.append(nested.get("code"))
    for value in candidates:
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 400 <= status <= 599:
            return status
    return None


class _SSEParser:
    def __init__(self):
        self._buffer = b""

    def feed(self, chunk):
        if not chunk:
            return
        self._buffer += chunk.replace(b"\r\n", b"\n")

    def events(self):
        while b"\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split(b"\n\n", 1)
            data = self._frame_data(frame)
            if data is None:
                continue
            try:
                payload = json.loads(data)
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            yield payload, data

    @staticmethod
    def _frame_data(frame):
        data_lines = []
        for line in frame.splitlines():
            if line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return None
        data = b"\n".join(data_lines)
        if data == b"[DONE]":
            return None
        return data.decode("utf-8", errors="replace")


class ResponsesWSBridge:
    def __init__(self, websocket, path, service, store):
        self.ws = websocket
        self.path = path
        self.service = service
        self.store = store
        self._replay_input = None
        self._pending_replay = []
        self._collected = []
        self._terminal = ""
        self._terminal_error_status = None
        self._buffered = None
        self._turn_sent = 0
        self.upstream = ""
        self.provider = ""
        self.remaining = ""
        self.client_ip = ""
        self.base_pool = None
        self.pool_credential_ok = False
        self.pool_access = False

    async def run(self):
        try:
            await self.ws.accept()
        except Exception:
            return
        query = self.ws.scope.get("query_string", b"") or b""
        self.path = (self.ws.scope.get("path", "") or "").lstrip("/")
        self.client_ip = self._request_ip()
        self.upstream, self.provider, self.remaining = match_route(self.path)
        if query:
            self.upstream = f"{self.upstream}?{query.decode('utf-8', errors='replace')}"
        self.base_pool = KEY_POOLS.get(self.upstream)
        self.pool_credential_ok = can_use_key_pool(self.ws.headers)
        self.pool_access = bool(self.base_pool and self.pool_credential_ok)
        if settings.proxy_api_key and self.pool_credential_ok and self.base_pool is None:
            await self._safe_close_error(503, "key_pool_unavailable",
                                         "Key pool is unavailable for this upstream")
            return
        logger.info(
            f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)}"
            f" 建立Responses WS连接 路由→{self.upstream.split('?', 1)[0]}"
        )
        turn = 0
        try:
            while True:
                if turn == 0:
                    timeout = settings.sse2ws_first_message_timeout
                else:
                    timeout = settings.sse2ws_inter_turn_idle_timeout
                kind, raw = await self._receive(timeout=timeout)
                if kind == "disconnect":
                    break
                if kind == "timeout":
                    await self.ws.close(1000, "websocket idle timeout")
                    break
                if kind == "binary":
                    await self._send_error(400, "binary frames are not supported", "invalid_request")
                    await self.ws.close(1008, "unsupported message type")
                    break
                if kind != "text":
                    await self.ws.close(1008, "protocol error")
                    break
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else None
                except (TypeError, ValueError):
                    payload = None
                if not isinstance(payload, dict):
                    await self._send_error(400, "request must be a JSON object", "invalid_request")
                    await self.ws.close(1008, "invalid request")
                    break
                msg_type = payload.get("type")
                if msg_type != "response.create":
                    if msg_type == "response.append":
                        message = ("response.append is not supported; "
                                   "use response.create with previous_response_id")
                    else:
                        message = f"unsupported message type: {msg_type}"
                    await self._send_error(400, message, "invalid_request")
                    await self.ws.close(1008, "unsupported message type")
                    break
                turn += 1
                keep = await self._run_turn_guarded(payload, turn)
                if not keep:
                    break
        finally:
            try:
                await self.ws.close(1000, "session ended")
            except Exception:
                pass

    def _request_ip(self):
        return resolve_client_ip(
            self.ws.scope, getattr(settings, "trusted_proxy_ips", ()),
        )

    async def _receive(self, timeout=None):
        if self._buffered is not None:
            raw = self._buffered
            self._buffered = None
            return ("text", raw)
        try:
            if timeout and timeout > 0:
                message = await asyncio.wait_for(self.ws.receive(), timeout=timeout)
            else:
                message = await self.ws.receive()
        except asyncio.TimeoutError:
            return ("timeout", None)
        except WebSocketDisconnect:
            return ("disconnect", None)
        mtype = message.get("type")
        if mtype == "websocket.disconnect":
            return ("disconnect", None)
        if mtype == "websocket.receive":
            text = message.get("text")
            if text is not None:
                return ("text", text)
            if message.get("bytes") is not None:
                return ("binary", None)
        return ("error", None)

    async def _watch_disconnect(self):
        while True:
            message = await self.ws.receive()
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                return
            if mtype == "websocket.receive":
                text = message.get("text")
                if text is not None and self._buffered is None:
                    self._buffered = text

    async def _run_turn_guarded(self, payload, turn):
        work = asyncio.create_task(self._run_turn(payload, turn))
        watcher = asyncio.create_task(self._watch_disconnect())
        done, _ = await asyncio.wait(
            {work, watcher}, return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return False
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        try:
            return work.result()
        except _ClientDisconnected:
            return False
        except Exception:
            logger.exception(f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)} turn 处理异常")
            return False

    async def _run_turn(self, payload, turn):
        token = set_client_ip(self.client_ip)
        start = time.time()
        try:
            return await self._run_turn_inner(payload, turn, start)
        except _ClientDisconnected:
            return False
        finally:
            reset_client_ip(token)

    async def _run_turn_inner(self, payload, turn, start):
        body_payload = self._prepare_turn_payload(payload)
        body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        if len(body) > settings.sse2ws_max_body_bytes:
            await self._safe_close_error(413, "request_body_too_large",
                                         "Request body exceeds the maximum allowed size")
            return False
        body, dlp_error = await self._apply_dlp(body)
        if dlp_error is not None:
            status, error_type, message = dlp_error
            await self._safe_close_error(status, error_type, message)
            return False
        model_name = body_payload.get("model") if isinstance(body_payload.get("model"), str) else ""
        session_id = parse_request_session_id(body)
        endpoint_family = classify_endpoint(self.remaining)
        model_scope = classify_model_scope(model_name, endpoint_family)
        outbound_headers = self._build_outbound_headers(model_name)
        url = build_proxy_url(self.upstream, self.remaining)
        request_pool = self._pick_pool(model_name, endpoint_family, model_scope)
        if self.pool_access and request_pool is None:
            await self._safe_close_error(
                403, "key_pool_no_match",
                "No compatible key pool route for this request",
            )
            return False
        logger.debug(
            f"{_tag('WS→SSE', self.path, self.provider, model_name, self.client_ip)}"
            f" turn #{turn} 开始"
        )
        input_items = _extract_input_items(body_payload)
        logger.debug(
            f"{_tag('WS→SSE', self.path, self.provider, model_name, self.client_ip)}"
            f" turn #{turn} 上游请求 body_bytes={len(body)} replay={'yes' if self._replay_input is not None else 'no'}"
            f" input_items={len(input_items)} input_types={[i.get('type') for i in input_items[:10]]}"
        )
        self._turn_sent = 0
        last_result = None
        last_outcome = None
        for attempt in range(1, settings.sse2ws_first_event_retries + 2):
            result = await self.service.request(
                "POST", url, outbound_headers, body, self.path,
                self.provider, model_name, request_pool, session_id,
                log_method="WS→SSE",
            )
            last_result = result
            self._turn_sent += result.total_sent
            response = result.response
            if response is None:
                last_outcome = {
                    "status": "upstream_exhausted", "succeeded": False,
                    "final_status": 503,
                    "error_type": "upstream_error",
                    "message": result.failure_reason
                    or "upstream overloaded after repeated attempts",
                }
                break
            if response.status_code >= 400:
                try:
                    raw = await response.aread()
                except Exception:
                    raw = b""
                try:
                    await response.aclose()
                except Exception:
                    pass
                message = self._extract_error_message(raw, response.status_code)
                last_outcome = {
                    "status": "error", "succeeded": False,
                    "final_status": response.status_code,
                    "error_type": "upstream_error",
                    "message": message,
                }
                break
            outcome = await self._consume_stream(result, request_pool, start, model_name, session_id)
            last_outcome = outcome
            if outcome.get("first_event_ok"):
                break
            if outcome.get("status") == "client_disconnected":
                return False
            if attempt <= settings.sse2ws_first_event_retries:
                logger.warning(
                    f"{_tag('WS→SSE', self.path, self.provider, model_name, self.client_ip)}"
                    f" turn #{turn} 首事件未到({outcome.get('status')}) 重试 #{attempt + 1}"
                    f" 总{time.time() - start:.1f}s"
                )
                await asyncio.sleep(settings.retry_interval)
                continue
        if last_outcome is None:
            last_outcome = {
                "status": "error", "succeeded": False, "final_status": 502,
                "error_type": "upstream_error", "message": "turn failed without result",
            }
        status = last_outcome.get("status")
        if status in SSE2WS_OK_TERMINAL_EVENTS:
            self._commit_replay()
        elif status == "first_event_timeout":
            self._neutralize_winner_key(last_result)
        elif status in ("eof", "transport_error", "response.failed",
                        "response.cancelled", "error"):
            self._handle_stream_failure(last_result, request_pool, session_id)
        succeeded = last_outcome.get("succeeded", False)
        await self._write_log(last_result, start, last_outcome, model_name)
        if status == "client_disconnected":
            return False
        if succeeded and status in SSE2WS_OK_TERMINAL_EVENTS:
            logger.info(
                f"{_tag('WS→SSE', self.path, self.provider, model_name, self.client_ip)}"
                f" turn #{turn} 结束 status={status} HTTP={last_outcome.get('final_status')}"
                f" 总{time.time() - start:.1f}s"
            )
            return True
        # 失败轮：保持连接以便客户端续聊/重试。若上游已发出终止事件
        # （failed/cancelled/error）则已透传，无需再补 error 帧。
        if not last_outcome.get("forwarded_terminal"):
            frame_status = last_outcome.get("final_status") or 502
            if status in ("eof", "transport_error"):
                frame_status = 502
            await self._send_error(
                frame_status,
                last_outcome.get("message") or "upstream request failed",
                str(last_outcome.get("error_type") or "upstream_error"),
            )
        logger.warning(
            f"{_tag('WS→SSE', self.path, self.provider, model_name, self.client_ip)}"
            f" turn #{turn} 失败 status={status} HTTP={last_outcome.get('final_status')}"
            f" 总{time.time() - start:.1f}s"
        )
        return True

    async def _apply_dlp(self, body):
        """DLP 策略与 HTTP 代理路径一致：audit/block/redact 与豁免标记。
        返回 (处理后的body, (status, error_type, message) 或 None)。"""
        if settings.dlp_mode not in ("audit", "block", "redact"):
            return body, None
        if len(body) > settings.dlp_max_body_bytes:
            logger.warning(
                f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)}"
                f" DLP请求体超限 bytes={len(body)}"
            )
            if settings.dlp_mode in ("block", "redact"):
                return body, (413, "dlp_body_too_large",
                              "Request body exceeds DLP inspection limit")
            return body, None
        dlp = await asyncio.to_thread(
            inspect_json_body, body, settings.dlp_rules,
            settings.dlp_exempt_start, settings.dlp_exempt_end,
            settings.dlp_strip_exempt_markers, settings.dlp_mode,
            settings.dlp_rule_file, None, settings.dlp_allow_exemptions,
            settings.dlp_decode_depth, settings.dlp_decode_max_candidates,
            settings.dlp_decode_max_bytes, _key_pool_secrets(),
            settings.dlp_known_secret_min_length,
        )
        if dlp.uninspectable and settings.dlp_fail_closed and body:
            return body, (422, "dlp_uninspectable_body",
                          "Request body cannot be inspected by DLP")
        if dlp.limit_exceeded:
            logger.warning(
                f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)} DLP解码扫描超限"
            )
            if settings.dlp_mode in ("block", "redact"):
                return body, (413, "dlp_decode_limit_exceeded",
                              "Request body exceeds DLP decode inspection limits")
            return body, None
        if dlp.malformed_exemption:
            logger.warning(
                f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)} DLP豁免标记不完整"
            )
            if settings.dlp_mode in ("block", "redact"):
                return body, (422, "dlp_malformed_exemption",
                              "Malformed DLP exemption markers")
            return body, None
        body = dlp.body
        if dlp.matched_rules:
            rules = ",".join(dlp.matched_rules)
            if dlp.blocked_rules:
                logger.warning(
                    f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)}"
                    f" DLP拦截 rules={','.join(dlp.blocked_rules)}"
                )
                return body, (422, "sensitive_data_blocked",
                              "Request blocked by sensitive data policy")
            action = "脱敏" if dlp.redactions else "告警"
            logger.warning(
                f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)}"
                f" DLP{action} rules={rules}"
            )
        if dlp.exemptions:
            logger.info(
                f"{_tag('WS→SSE', self.path, self.provider, '', self.client_ip)}"
                f" DLP豁免 count={dlp.exemptions}"
            )
        return body, None

    def _prepare_turn_payload(self, payload):
        current_items = _extract_input_items(payload)
        has_previous = bool((payload.get("previous_response_id") or "").strip())
        has_tool_output = any(
            isinstance(item, dict) and item.get("type") in TOOL_CALL_OUTPUT_TYPES
            for item in current_items
        )
        payload.pop("previous_response_id", None)
        needs_replay = has_previous or has_tool_output
        if needs_replay and self._replay_input is not None:
            if not current_items:
                full = self._replay_input
            elif not _items_have_prefix(current_items, self._replay_input):
                full = self._replay_input + current_items
            else:
                full = current_items
            payload["input"] = full
        payload.pop("type", None)
        payload.pop("generate", None)
        payload["stream"] = True
        self._pending_replay = _extract_input_items(payload)
        return payload

    def _commit_replay(self):
        merged = list(self._pending_replay)
        if self._collected:
            merged = merged + _dedup_context(self._collected)
        self._replay_input = merged

    def _build_outbound_headers(self, model_name):
        headers = outbound_request_headers(self.ws.headers, self.remaining, model_name)
        for name in SKIP_UPSTREAM_WS_HEADERS:
            headers.pop(name, None)
        for name in SKIP_UPSTREAM_WS_SESSION_HEADERS:
            headers.pop(name, None)
        beta = headers.get("openai-beta", "")
        if "responses_websockets" in beta:
            # 去掉 WS v2 专用 beta，避免上游按 WebSocket 协议处理 HTTP 请求。
            parts = [part.strip() for part in beta.split(",")
                     if part.strip() and "responses_websockets" not in part]
            if parts:
                headers["openai-beta"] = ",".join(parts)
            else:
                headers.pop("openai-beta", None)
        headers.pop("content-length", None)
        # 桥接自行构造 JSON 请求体，客户端握手的压缩声明不再适用
        headers.pop("content-encoding", None)
        headers["content-type"] = "application/json"
        return headers

    def _pick_pool(self, model_name, endpoint_family, model_scope):
        if self.base_pool is None or not self.pool_credential_ok:
            return None
        return self.base_pool.for_request(model_name, self.remaining, endpoint_family, model_scope)

    async def _consume_stream(self, result, request_pool, start, model_name, session_id):
        response = result.response
        parser = _SSEParser()
        queue = asyncio.Queue(maxsize=128)
        self._terminal = ""
        self._terminal_error_status = None
        self._collected = []

        async def producer():
            try:
                async for chunk in response.aiter_bytes():
                    parser.feed(chunk)
                    for payload, raw in parser.events():
                        await queue.put(("event", payload, raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(("error", {"_transport": str(exc)}, None))
            finally:
                try:
                    await queue.put(("eof", None, None))
                except Exception:
                    pass

        producer_task = asyncio.create_task(producer())
        outcome = {"first_event_ok": False}
        try:
            deadline = time.monotonic() + settings.sse2ws_first_event_timeout
            first_event_raw = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = {
                        "first_event_ok": False, "status": "first_event_timeout",
                        "succeeded": False, "final_status": 504,
                    }
                    break
                try:
                    kind, payload, raw = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    outcome = {
                        "first_event_ok": False, "status": "first_event_timeout",
                        "succeeded": False, "final_status": 504,
                    }
                    break
                if kind == "event":
                    first_event_raw = raw
                    self._handle_event(payload)
                    break
                if kind == "eof":
                    outcome = {
                        "first_event_ok": False, "status": "eof", "succeeded": False,
                        "final_status": response.status_code,
                        "message": "upstream stream ended before any event",
                    }
                    break
                outcome = {
                    "first_event_ok": False, "status": "transport_error",
                    "succeeded": False, "final_status": response.status_code,
                    "message": payload.get("_transport", "upstream transport error"),
                }
                break
            if first_event_raw is not None:
                try:
                    await self._send_text(first_event_raw)
                except _ClientDisconnected:
                    return {"status": "client_disconnected", "succeeded": False,
                            "final_status": None}
                outcome = {"first_event_ok": True}
                mid_error = None
                while True:
                    kind, payload, raw = await queue.get()
                    if kind == "event":
                        self._handle_event(payload)
                        try:
                            await self._send_text(raw)
                        except _ClientDisconnected:
                            return {"status": "client_disconnected", "succeeded": False,
                                    "final_status": None}
                    elif kind == "eof":
                        break
                    else:
                        mid_error = payload.get("_transport", "upstream transport error")
                        break
                if mid_error is not None:
                    outcome = {
                        "first_event_ok": True, "status": "transport_error",
                        "succeeded": False, "final_status": response.status_code,
                        "message": mid_error,
                    }
                else:
                    outcome = self._finish_stream_outcome(response.status_code)
                    outcome["first_event_ok"] = True
        finally:
            producer_task.cancel()
            await asyncio.gather(producer_task, return_exceptions=True)
            try:
                await response.aclose()
            except Exception:
                pass
        return outcome

    def _finish_stream_outcome(self, http_status):
        terminal = self._terminal
        if terminal in SSE2WS_OK_TERMINAL_EVENTS:
            return {"status": terminal, "succeeded": True, "final_status": http_status}
        if terminal in ("response.failed", "response.cancelled", "error"):
            return {"status": terminal, "succeeded": False, "final_status": http_status,
                    "forwarded_terminal": True}
        return {"status": "eof", "succeeded": False, "final_status": http_status,
                "message": "upstream stream ended before a terminal event"}

    def _handle_event(self, payload):
        event_type = payload.get("type")
        if event_type in SSE2WS_TERMINAL_EVENTS:
            self._terminal = event_type
            status = _embedded_failure_status(payload)
            if status is not None:
                self._terminal_error_status = status
        if event_type == "response.output_item.done":
            item = payload.get("item")
            if _is_context_item(item):
                self._collected.append(item)
        elif event_type in ("response.completed", "response.done"):
            response = payload.get("response")
            if isinstance(response, dict):
                for item in response.get("output") or []:
                    if _is_context_item(item):
                        self._collected.append(item)

    def _neutralize_winner_key(self, result):
        if result is None or result.key_entry is None:
            return
        for attempt in reversed(result.key_attempts or []):
            if attempt.get("key_id") == result.key_entry.key_id:
                attempt["available"] = None
                return

    def _handle_stream_failure(self, result, request_pool, session_id):
        status = self._terminal_error_status
        if result is None or result.key_entry is None:
            return
        if status in (401, 403, 429) and request_pool is not None:
            for attempt in reversed(result.key_attempts or []):
                if attempt.get("key_id") == result.key_entry.key_id:
                    attempt["available"] = False
                    break
            _mark_key_failure(request_pool, result.key_entry, settings, status,
                              session_id=session_id)
        else:
            self._neutralize_winner_key(result)

    async def _send_text(self, text):
        try:
            await self.ws.send_text(text)
        except Exception as exc:
            raise _ClientDisconnected from exc

    async def _send_error(self, status, message, error_type="server_error"):
        try:
            await self.ws.send_text(_build_error_event(status, message, error_type))
        except Exception:
            raise _ClientDisconnected

    async def _safe_close_error(self, status, error_type, message):
        try:
            await self._send_error(status, message, error_type)
        except _ClientDisconnected:
            return
        try:
            await self.ws.close(1011, "upstream error")
        except Exception:
            pass

    @staticmethod
    def _extract_error_message(raw, status_code):
        text = ""
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, UnicodeDecodeError):
                text = raw.decode("utf-8", errors="replace")
            else:
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        text = error.get("message") or error.get("detail") or ""
                    if not text:
                        text = payload.get("message") or payload.get("detail") or ""
                    if not text and isinstance(error, str):
                        text = error
        message = " ".join(text.split())[:300] if text else ""
        return message or f"upstream returned HTTP {status_code}"

    async def _write_log(self, result, start, outcome, model_name):
        attempts = self._turn_sent if result is not None else 1
        record = {
            "method": "WS→SSE",
            "path": "/" + self.path,
            "provider": self.provider,
            "model": model_name,
            "upstream_status": outcome.get("final_status") or 0,
            "attempts": attempts,
            "retries": max(attempts - 1, 0),
            "retry_codes": (result.retry_codes if result is not None else []) or [],
            "mode": "sse2ws-bridge",
            "first_ok": result.first_ok if result is not None else False,
            "key_id": result.key_id if result is not None else "",
            "key_pool": self.upstream if result is not None and result.key_id else "",
            "key_attempts": (result.key_attempts if result is not None else []) or [],
            "client_ip": self.client_ip,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "final_status": outcome.get("final_status") or 0,
            "duration_s": round(time.time() - start, 3),
            "succeeded": outcome.get("succeeded", False),
            "stream_status": outcome.get("status", ""),
        }
        await self.store.write(record)


def create_sse2ws_handler(service, store):
    async def handler(websocket: WebSocket):
        path = (websocket.scope.get("path", "") or "").lstrip("/")
        if not is_responses_ws_path(path):
            try:
                await websocket.close(1008)
            except Exception:
                pass
            return
        bridge = ResponsesWSBridge(websocket, path, service, store)
        await bridge.run()

    return handler
