import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import httpx
import websockets
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .config import can_use_key_pool, log_capture, logger, settings
from .dlp import inspect_json_body
from .routes import ROUTES, is_excluded_path, match_route
from .key_pool import KEY_POOLS, headers_with_key
from .retry import (KeyPoolWaitTimeout, _mark_key_failure, _pick_key, filter_headers,
                    parse_model, reset_client_ip, set_client_ip, _tag)
from .stats import _normalize_provider, _req_succeeded, _upstream_window_stats, compute_key_pool_stats, compute_stats

SKIP_REQUEST_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"}
SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}
SKIP_WEBSOCKET_HEADERS = SKIP_REQUEST_HEADERS | {
    "origin", "sec-websocket-accept", "sec-websocket-extensions", "sec-websocket-key",
    "sec-websocket-protocol", "sec-websocket-version",
}

_GEMINI_MODEL_PATH = re.compile(
    r"(?:^|/)models/([^/:]+):(?:generatecontent|streamgeneratecontent|embedcontent|batchgeneratecontent)(?:/|$)",
    re.IGNORECASE,
)


def classify_endpoint(path):
    """Return a stable endpoint family for capability-aware key routing."""
    normalized = (path or "").strip("/").lower()
    if not normalized:
        return ""
    if _GEMINI_MODEL_PATH.search(normalized):
        return "gemini"
    segments = normalized.split("/")
    if "images" in segments:
        return "images"
    if len(segments) >= 2 and segments[-2:] == ["chat", "completions"]:
        return "chat"
    for family in ("responses", "messages", "embeddings", "audio"):
        if family in segments:
            return family
    return ""


def parse_request_model(body, path=""):
    model = parse_model(body)
    if model:
        return model
    match = _GEMINI_MODEL_PATH.search((path or "").strip("/"))
    return unquote(match.group(1)) if match else ""


def classify_model_scope(model, endpoint_family=""):
    value = (model or "").strip().lower()
    if value.startswith("claude"):
        return "claude"
    if value.startswith("gemini"):
        image_markers = ("image", "imagen", "nano-banana")
        if endpoint_family == "images" or any(marker in value for marker in image_markers):
            return "gemini_image"
        return "gemini_text"
    return ""


def _json_has_token(payload):
    candidates = []
    known_stream_shape = False
    if isinstance(payload, dict):
        event_type = payload.get("type")
        known_stream_shape = (
            "choices" in payload or "delta" in payload
            or isinstance(event_type, str) and event_type.startswith((
                "response.", "content_block_", "message_", "session.",
            ))
        )
        candidates.extend((payload.get("delta"), payload.get("text")))
        for choice in payload.get("choices") or []:
            if isinstance(choice, dict):
                delta = choice.get("delta")
                candidates.append(delta.get("content") if isinstance(delta, dict) else delta)
                candidates.append(choice.get("text"))
        nested = payload.get("delta")
        if isinstance(nested, dict):
            candidates.extend((nested.get("text"), nested.get("content")))
    if any(isinstance(value, str) and value for value in candidates):
        return True
    return not known_stream_shape


def _sse_has_token(buffer):
    while b"\n\n" in buffer:
        event, buffer = buffer.split(b"\n\n", 1)
        data = b"\n".join(line[5:].strip() for line in event.splitlines()
                           if line.startswith(b"data:"))
        if not data or data == b"[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            return True, buffer
        if _json_has_token(payload):
            return True, buffer
    return False, buffer


def _websocket_message_has_token(message):
    if not message:
        return False
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return True
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return True
    if isinstance(payload, dict) and payload.get("type") in (
            "error", "response.error", "response.failed", "response.incomplete"):
        return False
    return _json_has_token(payload)


def _websocket_request_info(message):
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return "", False, False
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return "", False, False
    if not isinstance(payload, dict) or payload.get("type") != "response.create":
        return "", False, False
    model = payload.get("model")
    return model if isinstance(model, str) else "", payload.get("generate") is not False, True


def _websocket_url(upstream, remaining, query=""):
    if upstream.startswith("https://"):
        upstream = "wss://" + upstream[8:]
    elif upstream.startswith("http://"):
        upstream = "ws://" + upstream[7:]
    elif not upstream.startswith(("ws://", "wss://")):
        raise ValueError("WebSocket upstream must use http(s) or ws(s)")
    url = f"{upstream}/{remaining}" if remaining else upstream
    return f"{url}?{query}" if query else url


def outbound_request_headers(request_headers, path, model, config=settings):
    headers = filter_headers(request_headers, SKIP_REQUEST_HEADERS)
    image_request = ("/images/" in f"/{(path or '').lower()}"
                     or (model or "").lower().startswith(("gpt-image-", "image")))
    if image_request and config.image_upstream_user_agent:
        headers["user-agent"] = config.image_upstream_user_agent
    if image_request and config.image_upstream_originator:
        headers["originator"] = config.image_upstream_originator
    return headers


def _inspect_websocket_message(body, path, provider, client_ip):
    if settings.dlp_mode not in ("audit", "block", "redact"):
        return body, ""
    tag = _tag("WS", path, provider, "", client_ip)
    if len(body) > settings.dlp_max_body_bytes:
        logger.warning(f"{tag} DLP消息超限 bytes={len(body)}")
        if settings.dlp_mode in ("block", "redact"):
            return body, "WebSocket message exceeds DLP inspection limit"
        return body, ""
    dlp = inspect_json_body(
        body, settings.dlp_rules, settings.dlp_exempt_start, settings.dlp_exempt_end,
        settings.dlp_strip_exempt_markers, mode=settings.dlp_mode,
        rule_file=settings.dlp_rule_file, allow_exemptions=settings.dlp_allow_exemptions,
        decode_depth=settings.dlp_decode_depth,
        decode_max_candidates=settings.dlp_decode_max_candidates,
        decode_max_bytes=settings.dlp_decode_max_bytes, known_secrets=_key_pool_secrets(),
        known_secret_min_length=settings.dlp_known_secret_min_length,
    )
    if dlp.uninspectable and settings.dlp_fail_closed and body:
        logger.warning(f"{tag} DLP无法解析消息")
        return body, "WebSocket message cannot be inspected by DLP"
    if dlp.limit_exceeded and settings.dlp_mode in ("block", "redact"):
        logger.warning(f"{tag} DLP解码扫描超限")
        return body, "WebSocket message exceeds DLP decode inspection limits"
    if dlp.malformed_exemption and settings.dlp_mode in ("block", "redact"):
        logger.warning(f"{tag} DLP豁免标记不完整")
        return body, "Malformed DLP exemption markers"
    if dlp.blocked_rules:
        logger.warning(f"{tag} DLP拦截 rules={','.join(dlp.blocked_rules)}")
        return body, "WebSocket message blocked by sensitive data policy"
    if dlp.matched_rules:
        action = "脱敏" if dlp.redactions else "告警"
        logger.warning(f"{tag} DLP{action} rules={','.join(dlp.matched_rules)}")
    return dlp.body, ""


async def _run_until_disconnect(request, awaitable):
    work = asyncio.create_task(awaitable)

    async def watch_disconnect():
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return

    watcher = asyncio.create_task(watch_disconnect())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        return None
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)


def _summary_view(summary):
    out = []
    for name, b in summary.items():
        out.append({"name": name, "requests": b["requests"], "retries": b["retries"],
                    "avg_retries": round(b["retries"] / b["requests"], 2) if b["requests"] else 0,
                    "availability_pct": round(b["succeeded"] / b["requests"] * 100, 2) if b["requests"] else 0,
                    "upstream_availability_pct": round(b.get("first_ok", 0) / b["requests"] * 100, 2) if b["requests"] else 0,
                    "max_retries": b["max_retries"]})
    return sorted(out, key=lambda x: x["requests"], reverse=True)


def _cumulative(summary):
    total = summary["total_requests"]
    return {"total_requests": total, "total_retries": summary["total_retries"],
            "avg_retries": round(summary["total_retries"] / total, 2) if total else 0,
            "availability_pct": round(summary["total_succeeded"] / total * 100, 2) if total else 0,
            "upstream_availability_pct": round(summary.get("total_first_ok", 0) / total * 100, 2) if total else 0,
            "succeeded": summary["total_succeeded"], "failed": summary["total_failed"],
            "by_provider": _summary_view(summary["by_provider"]), "by_model": _summary_view({k: v for k, v in summary["by_model"].items() if not k.endswith("/(unknown)")}),
            "by_key": _summary_view(summary.get("by_key", {})),
            "by_status": [{"status": k, "count": v} for k, v in sorted(summary["by_status"].items(), key=lambda x: -x[1])],
            "first_ts": summary.get("first_ts"), "last_ts": summary.get("last_ts")}


def _request_ip(request):
    for header in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _key_pool_secrets():
    return tuple(entry.key for pool in KEY_POOLS.values() for entry in pool.entries)


def create_handlers(service, store):
    async def health():
        return {"status": "ok"}

    async def stats_page():
        if os.path.exists(settings.stats_html_path):
            with open(settings.stats_html_path, encoding="utf-8") as f: return Response(f.read(), media_type="text/html; charset=utf-8")
        return Response("stats.html not found", status_code=404)

    async def stats_api(range="today", model="", provider="", plan_start="", rate_mode=""):
        days = {"today": 1, "7d": 7, "30d": 30, "all": 0}.get(range, 1)
        records = store.load(days)
        selected_models = {m.strip() for m in model.split(",") if m.strip()} if model else set()
        selected_providers = {p.strip() for p in provider.split(",") if p.strip()} if provider else set()
        available_providers = sorted({r.get("provider", "") for r in records if r.get("provider")})
        if selected_providers:
            records = [r for r in records if r.get("provider") in selected_providers]
        available_models = sorted({r.get("model", "") for r in records if r.get("model")})
        if selected_models:
            records = [r for r in records if r.get("model") in selected_models]
        window = store.load(2); rate = store.load(30)
        if selected_providers:
            window = [r for r in window if r.get("provider") in selected_providers]
            rate = [r for r in rate if r.get("provider") in selected_providers]
        if selected_models:
            window = [r for r in window if r.get("model") in selected_models]
            rate = [r for r in rate if r.get("model") in selected_models]
        now = datetime.now(); ps_dt = None
        if plan_start:
            try: ps_dt = datetime.fromisoformat(plan_start)
            except (ValueError, TypeError): pass
        def count_since(cutoff):
            count = 0
            for record in rate:
                try:
                    if datetime.fromisoformat(record.get("ts", "")) >= cutoff and _req_succeeded(record): count += 1
                except (ValueError, TypeError): pass
            return count
        def sliding(): return count_since(now - timedelta(hours=5)), count_since(now - timedelta(days=7)), count_since(now - timedelta(days=30))
        if rate_mode == "platform" and ps_dt:
            whole_hour = now.replace(minute=0, second=0, microsecond=0)
            c5h = count_since(max(whole_hour - timedelta(hours=5), ps_dt))
            today_0800 = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now < today_0800: today_0800 -= timedelta(days=1)
            c_week = count_since(max(today_0800 - timedelta(days=7), ps_dt)); c_month = count_since(ps_dt)
        elif rate_mode == "fixed" and ps_dt:
            elapsed_days = max(0, (now - ps_dt).days); elapsed_5h = max(0, int((now - ps_dt).total_seconds() // 3600 // 5))
            c5h = count_since(ps_dt + timedelta(hours=elapsed_5h * 5)); c_week = count_since(ps_dt + timedelta(days=(elapsed_days // 7) * 7)); c_month = count_since(ps_dt + timedelta(days=(elapsed_days // 30) * 30))
        else: c5h, c_week, c_month = sliding()
        cfg = {"provider": settings.provider, "upstream_url": settings.upstream_url,
               "routes": [{"prefix": p or "/", "upstream": u, "provider": pv} for p, u, pv, _ in ROUTES],
               "retry_status_codes": sorted(settings.retry_status_codes), "retry_interval": settings.retry_interval,
               "retry_interval_429": settings.retry_interval_429, "retry_backoff": settings.retry_backoff,
               "retry_backoff_max": settings.retry_backoff_max, "retry_backoff_429": settings.retry_backoff_429,
               "retry_backoff_max_429": settings.retry_backoff_max_429, "max_retries": settings.max_retries, "timeout": settings.timeout}
        pool_configs = []
        for url, pool in KEY_POOLS.items():
            pool_provider = _normalize_provider(pool.provider or settings.provider)
            if not selected_providers or pool_provider in selected_providers:
                pool_configs.append({"id": url, "upstream": url, "provider": pool_provider,
                                     "keys": pool.status()})
        return {"detail": compute_stats(records, range, cfg), "cumulative": _cumulative(store.summary), "range": range,
                "record_count": len(records), "available_models": available_models,
                "available_providers": available_providers, "upstream_windows": _upstream_window_stats(window),
                "key_pools": compute_key_pool_stats(records, pool_configs, health_records=window),
                "rate_counts": {"5h": c5h, "week": c_week, "month": c_month}}

    async def logs_page():
        if os.path.exists(settings.logs_html_path):
            with open(settings.logs_html_path, encoding="utf-8") as f:
                return Response(f.read(), media_type="text/html; charset=utf-8")
        return Response("logs.html not found", status_code=404)

    async def logs_history(since: int = 0):
        entries = log_capture.history()
        if since > 0:
            entries = [e for e in entries if e.get("seq", 0) > since]
        return entries

    async def logs_stream(request: Request, since: int = 0):
        async def event_gen():
            q = log_capture.subscribe()
            try:
                for entry in log_capture.history():
                    if entry.get("seq", 0) > since:
                        yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        entry = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                log_capture.unsubscribe(q)
        return StreamingResponse(event_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    async def proxy(path: str, request: Request):
        if is_excluded_path(path): return Response(status_code=404)
        client_ip = _request_ip(request)
        upstream, provider, remaining = match_route(path); url = f"{upstream}/{remaining}" if remaining else upstream
        if request.url.query: url += f"?{request.url.query}"
        logger.debug(f"{_tag(request.method, path, provider, '', client_ip)} 收到下游请求")
        body = await request.body() if request.method not in ("GET", "HEAD") else b""
        if settings.dlp_mode in ("audit", "block", "redact"):
            if len(body) > settings.dlp_max_body_bytes:
                logger.warning(f"{_tag(request.method, path, provider, '', client_ip)} DLP请求体超限 bytes={len(body)}")
                if settings.dlp_mode in ("block", "redact"):
                    return Response('{"error":{"type":"dlp_body_too_large","message":"Request body exceeds DLP inspection limit"}}', status_code=413, media_type="application/json")
            else:
                dlp = inspect_json_body(body, settings.dlp_rules, settings.dlp_exempt_start,
                                        settings.dlp_exempt_end, settings.dlp_strip_exempt_markers,
                                        mode=settings.dlp_mode,
                                        rule_file=settings.dlp_rule_file,
                                        allow_exemptions=settings.dlp_allow_exemptions,
                                        decode_depth=settings.dlp_decode_depth,
                                        decode_max_candidates=settings.dlp_decode_max_candidates,
                                        decode_max_bytes=settings.dlp_decode_max_bytes,
                                        known_secrets=_key_pool_secrets(),
                                        known_secret_min_length=settings.dlp_known_secret_min_length)
                if dlp.uninspectable and settings.dlp_fail_closed and body:
                    logger.warning(f"{_tag(request.method, path, provider, '', client_ip)} DLP无法解析请求正文")
                    return Response(
                        '{"error":{"type":"dlp_uninspectable_body","message":"Request body cannot be inspected by DLP"}}',
                        status_code=422, media_type="application/json",
                    )
                if dlp.limit_exceeded:
                    logger.warning(f"{_tag(request.method, path, provider, '', client_ip)} DLP解码扫描超限")
                    if settings.dlp_mode in ("block", "redact"):
                        return Response(
                            '{"error":{"type":"dlp_decode_limit_exceeded","message":"Request body exceeds DLP decode inspection limits"}}',
                            status_code=413, media_type="application/json",
                        )
                if dlp.malformed_exemption:
                    logger.warning(f"{_tag(request.method, path, provider, '', client_ip)} DLP豁免标记不完整")
                    if settings.dlp_mode in ("block", "redact"):
                        return Response('{"error":{"type":"dlp_malformed_exemption","message":"Malformed DLP exemption markers"}}', status_code=422, media_type="application/json")
                else:
                    body = dlp.body
                if dlp.matched_rules:
                    rules = ",".join(dlp.matched_rules)
                    if dlp.blocked_rules:
                        logger.warning(f"{_tag(request.method, path, provider, '', client_ip)} DLP拦截 rules={','.join(dlp.blocked_rules)}")
                        payload = json.dumps({"error": {"type": "sensitive_data_blocked",
                                             "message": "Request blocked by sensitive data policy",
                                             "rules": list(dlp.blocked_rules)}}, ensure_ascii=False)
                        return Response(payload, status_code=422, media_type="application/json")
                    action = "脱敏" if dlp.redactions else "告警"
                    count = f" count={dlp.redactions}" if dlp.redactions else ""
                    logger.warning(f"{_tag(request.method, path, provider, '', client_ip)} DLP{action} rules={rules}{count}")
                if dlp.exemptions:
                    logger.info(f"{_tag(request.method, path, provider, '', client_ip)} DLP豁免 count={dlp.exemptions}")
        endpoint_family = classify_endpoint(remaining)
        model_name = parse_request_model(body, remaining)
        model_scope = classify_model_scope(model_name, endpoint_family)
        outbound_headers = outbound_request_headers(request.headers, remaining, model_name)
        base_pool = KEY_POOLS.get(upstream)
        pool_credential_ok = can_use_key_pool(request.headers)
        if settings.proxy_api_key and pool_credential_ok and base_pool is None:
            return Response(
                '{"error":{"type":"key_pool_unavailable","message":"Key pool is unavailable for this upstream"}}',
                status_code=503, media_type="application/json",
            )
        pool_access = bool(base_pool and pool_credential_ok)
        capability_routing = bool(
            pool_access and endpoint_family and base_pool.has_routing_capabilities()
        )
        request_pool = base_pool.for_request(
            model_name, remaining, endpoint_family, model_scope,
        ) if pool_access else None
        if pool_access and request_pool is None:
            error_type = (
                "key_pool_no_compatible_route" if capability_routing else "key_pool_no_match"
            )
            message = (
                "No compatible key pool route for this endpoint and model"
                if capability_routing else "No key pool entry matches this request"
            )
            payload = json.dumps({"error": {
                "type": error_type,
                "message": message,
                "model": model_name,
                "endpoint_family": endpoint_family,
                "provider": provider,
                "upstream": upstream,
                "reason": "capability_mismatch" if capability_routing else "manual_rule_mismatch",
            }}, ensure_ascii=False)
            return Response(payload, status_code=403, media_type="application/json")
        key_pool = upstream if request_pool else ""
        ip_token = set_client_ip(client_ip)
        try:
            result = await _run_until_disconnect(
                request,
                service.request(request.method, url, outbound_headers,
                                body, path, provider, model_name, request_pool),
            )
        finally:
            reset_client_ip(ip_token)
        if result is None:
            return Response(status_code=499)
        response = result.response
        winner_attempt = result.winner_attempt
        total_sent = result.total_sent
        last_status = result.last_status
        retry_codes = result.retry_codes
        first_ok = result.first_ok
        key_id = result.key_id
        key_attempts = getattr(result, "key_attempts", None) or []
        start = result.started_at
        await store.write({"ts": datetime.now().isoformat(timespec="milliseconds"), "method": request.method,
                           "path": "/" + path, "provider": provider, "model": model_name,
                           "upstream_status": last_status, "final_status": response.status_code if response else 503,
                           "attempts": total_sent, "retries": max(total_sent - 1, 0),
                           "duration_s": round(time.time() - start, 3), "succeeded": bool(response and response.status_code < 400),
                           "retry_codes": retry_codes, "mode": service.hedge_mode_for(request_pool), "first_ok": first_ok,
                           "key_id": key_id, "key_pool": key_pool, "key_attempts": key_attempts,
                           "client_ip": client_ip})
        if response is None:
            logger.error(f"{_tag(request.method, path, provider, model_name, client_ip)} 放弃({total_sent}发) {time.time() - start:.1f}s")
            reason = getattr(result, "failure_reason", "")
            message = reason or f"upstream overloaded after {total_sent} attempts"
            payload = json.dumps({"error": {"message": message, "type": "upstream_error", "code": "503"}}, ensure_ascii=False)
            return Response(payload, status_code=503, media_type="application/json", headers={"X-Forward-Attempts": str(total_sent)})
        headers = filter_headers(response.headers, SKIP_RESPONSE_HEADERS); headers["X-Forward-Attempts"] = str(winner_attempt)
        # 流式响应禁用反向代理缓冲，否则 nginx/群晖反代会攒批 flush 导致远程访问"一顿一顿"
        if "event-stream" in response.headers.get("content-type", "") or response.headers.get("content-length") is None:
            headers["X-Accel-Buffering"] = "no"; headers["Cache-Control"] = "no-cache"
        async def body_gen():
            probe_buffer = b""
            ttft_recorded = False
            is_sse = "event-stream" in response.headers.get("content-type", "")
            try:
                async for chunk in response.aiter_bytes():
                    if not ttft_recorded and response.status_code < 400 and chunk:
                        if is_sse:
                            probe_buffer += chunk.replace(b"\r\n", b"\n")
                            if len(probe_buffer) > 65536:
                                probe_buffer = probe_buffer[-65536:]
                            found, probe_buffer = _sse_has_token(probe_buffer)
                        else:
                            found = True
                        if found:
                            ttft_recorded = True
                            entry = getattr(result, "key_entry", None)
                            sent_at = getattr(result, "response_started_at", 0.0)
                            if request_pool is not None and entry is not None and sent_at > 0:
                                request_pool.record_ttft(entry, time.time() - sent_at)
                    yield chunk
            except httpx.TransportError as e:
                logger.warning(f"{_tag(request.method, path, provider, model_name, client_ip)} 流式中断 #{winner_attempt} {e!r} 总{time.time() - start:.2f}s")
            finally:
                await response.aclose()
        return StreamingResponse(body_gen(), status_code=response.status_code, headers=headers, media_type=response.headers.get("content-type"))
    return health, stats_page, stats_api, logs_page, logs_history, logs_stream, proxy


def create_websocket_handler(store):
    async def websocket_proxy(websocket: WebSocket, path: str):
        if is_excluded_path(path):
            await websocket.close(code=1008)
            return
        client_ip = _request_ip(websocket)
        upstream, provider, remaining = match_route(path)
        try:
            url = _websocket_url(upstream, remaining, websocket.url.query)
        except ValueError:
            await websocket.close(code=1011)
            return
        base_pool = KEY_POOLS.get(upstream)
        pool_credential_ok = can_use_key_pool(websocket.headers)
        if settings.proxy_api_key and pool_credential_ok and base_pool is None:
            await websocket.close(code=1013, reason="Key pool is unavailable for this upstream")
            return
        headers = filter_headers(websocket.headers, SKIP_WEBSOCKET_HEADERS)
        origin = websocket.headers.get("origin")
        await websocket.accept()
        try:
            first_message = await websocket.receive()
        except WebSocketDisconnect:
            return
        if first_message["type"] == "websocket.disconnect":
            return
        first_value = first_message.get("text")
        first_binary = first_value is None
        if first_binary:
            first_value = first_message.get("bytes", b"")
        first_raw = first_value if isinstance(first_value, bytes) else first_value.encode("utf-8")
        first_raw, dlp_error = _inspect_websocket_message(
            first_raw, path, provider, client_ip,
        )
        if dlp_error:
            await websocket.close(code=1008, reason=dlp_error[:120])
            return
        first_outbound = first_raw if first_binary else first_raw.decode("utf-8")
        model, first_generates, _ = _websocket_request_info(first_outbound)
        if not model:
            model = websocket.query_params.get("model", "")
        endpoint_family = classify_endpoint(remaining)
        model_scope = classify_model_scope(model, endpoint_family)
        pool_access = bool(base_pool and pool_credential_ok)
        request_pool = base_pool.for_request(
            model, remaining, endpoint_family, model_scope,
        ) if pool_access else None
        if pool_access and request_pool is None:
            await websocket.close(code=1008, reason="No compatible key pool route")
            return
        try:
            entry = await _pick_key(
                request_pool, getattr(settings, "key_pool_wait_timeout", None),
            )
        except KeyPoolWaitTimeout:
            await websocket.close(code=1013, reason="All compatible keys are cooling down")
            return
        if entry:
            headers = headers_with_key(headers, entry.key, entry.auth_header, entry.auth_scheme)
        started_at = time.time()
        logger.debug(f"{_tag('WS', path, provider, model, client_ip)} 连接上游")
        try:
            upstream_ws = await websockets.connect(
                url, additional_headers=headers, origin=origin,
                open_timeout=settings.connect_timeout, close_timeout=10, max_size=None,
                user_agent_header=None if "user-agent" in headers else "Python/websockets",
                proxy=True if settings.trust_env else None,
            )
        except InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if entry and (status in (401, 403, 429) or status >= 500):
                _mark_key_failure(request_pool, entry, settings, status)
            await websocket.close(code=1013, reason=f"Upstream WebSocket handshake failed ({status or 'HTTP'})")
            return
        except (OSError, TimeoutError, websockets.WebSocketException):
            await websocket.close(code=1013, reason="Upstream WebSocket connection failed")
            return

        if entry:
            request_pool.mark_success(entry)
        logger.info(f"{_tag('WS', path, provider, model, client_ip)} 已连接")
        request_started_at = time.time() if first_generates else 0.0
        ttft_recorded = not first_generates
        await upstream_ws.send(first_outbound)

        async def downstream_to_upstream():
            nonlocal request_started_at, ttft_recorded
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                value = message.get("text")
                binary = value is None
                if binary:
                    value = message.get("bytes", b"")
                raw = value if isinstance(value, bytes) else value.encode("utf-8")
                inspected, error = _inspect_websocket_message(
                    raw, path, provider, client_ip,
                )
                if error:
                    await websocket.close(code=1008, reason=error[:120])
                    return
                outbound = inspected if binary else inspected.decode("utf-8")
                next_model, generates, is_response_create = _websocket_request_info(outbound)
                if is_response_create:
                    if next_model and base_pool is not None and entry is not None:
                        compatible_pool = base_pool.for_request(
                            next_model, remaining, endpoint_family,
                            classify_model_scope(next_model, endpoint_family),
                        )
                        if compatible_pool is None or entry not in compatible_pool.entries:
                            await websocket.close(
                                code=1008,
                                reason="Model is incompatible with the selected key pool route",
                            )
                            return
                    request_started_at = time.time() if generates else 0.0
                    ttft_recorded = not generates
                await upstream_ws.send(outbound)

        async def upstream_to_downstream():
            nonlocal ttft_recorded
            async for message in upstream_ws:
                if (not ttft_recorded and request_started_at > 0
                        and _websocket_message_has_token(message)):
                    ttft_recorded = True
                    if request_pool is not None and entry is not None:
                        request_pool.record_ttft(entry, time.time() - request_started_at)
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        tasks = {
            asyncio.create_task(downstream_to_upstream()),
            asyncio.create_task(upstream_to_downstream()),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream_ws.close()
            if websocket.client_state.name != "DISCONNECTED":
                try:
                    await websocket.close()
                except RuntimeError:
                    pass
        await store.write({
            "ts": datetime.now().isoformat(timespec="milliseconds"), "method": "WS",
            "path": "/" + path, "provider": provider, "model": model,
            "upstream_status": 101, "final_status": 101, "attempts": 1, "retries": 0,
            "duration_s": round(time.time() - started_at, 3), "succeeded": True,
            "retry_codes": [], "mode": "off", "first_ok": True,
            "key_id": entry.key_id if entry else "", "key_pool": upstream if entry else "",
            "key_attempts": [], "client_ip": client_ip,
        })

    return websocket_proxy
