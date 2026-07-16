import asyncio
import json
import os
import time
from datetime import datetime, timedelta

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .config import log_capture, logger, settings
from .routes import ROUTES, is_excluded_path, match_route
from .key_pool import KEY_POOLS
from .retry import filter_headers, parse_model, _tag
from .stats import _req_succeeded, _upstream_window_stats, compute_stats

SKIP_REQUEST_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"}
SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}


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


def create_handlers(service, store):
    async def health():
        return {"status": "ok", "upstream": settings.upstream_url,
                "routes": [{"prefix": p or "/", "upstream": u, "provider": pv} for p, u, pv, _ in ROUTES],
                "key_pools": {u: {"provider": p.provider or settings.provider, "keys": p.status()} for u, p in KEY_POOLS.items()}}

    async def stats_page():
        if os.path.exists(settings.stats_html_path):
            with open(settings.stats_html_path, encoding="utf-8") as f: return Response(f.read(), media_type="text/html; charset=utf-8")
        return Response("stats.html not found", status_code=404)

    async def stats_api(range="today", model="", plan_start="", rate_mode=""):
        days = {"today": 1, "7d": 7, "30d": 30, "all": 0}.get(range, 1)
        records = store.load(days); selected = {m.strip() for m in model.split(",") if m.strip()} if model else set()
        available = sorted({r.get("model", "") for r in records if r.get("model")})
        if selected: records = [r for r in records if r.get("model") in selected]
        window = store.load(2); rate = store.load(30)
        if selected: window = [r for r in window if r.get("model") in selected]; rate = [r for r in rate if r.get("model") in selected]
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
        return {"detail": compute_stats(records, range, cfg), "cumulative": _cumulative(store.summary), "range": range,
                "record_count": len(records), "available_models": available, "upstream_windows": _upstream_window_stats(window), "rate_counts": {"5h": c5h, "week": c_week, "month": c_month}}

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

    async def logs_stream(request: Request):
        async def event_gen():
            q = log_capture.subscribe()
            try:
                for entry in log_capture.history():
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
        upstream, provider, remaining = match_route(path); url = f"{upstream}/{remaining}" if remaining else upstream
        if request.url.query: url += f"?{request.url.query}"
        body = await request.body() if request.method not in ("GET", "HEAD") else b""
        model_name = parse_model(body)
        result = await service.request(request.method, url, filter_headers(request.headers, SKIP_REQUEST_HEADERS), body, path, provider, model_name, KEY_POOLS.get(upstream))
        response = result.response
        winner_attempt = result.winner_attempt
        total_sent = result.total_sent
        last_status = result.last_status
        retry_codes = result.retry_codes
        first_ok = result.first_ok
        key_id = result.key_id
        start = result.started_at
        await store.write({"ts": datetime.now().isoformat(timespec="milliseconds"), "method": request.method,
                           "path": "/" + path, "provider": provider, "model": model_name,
                           "upstream_status": last_status, "final_status": response.status_code if response else 503,
                           "attempts": total_sent, "retries": max(total_sent - 1, 0),
                           "duration_s": round(time.time() - start, 3), "succeeded": bool(response and response.status_code < 400),
                            "retry_codes": retry_codes, "mode": settings.hedge_mode, "first_ok": first_ok, "key_id": key_id})
        if response is None:
            logger.error(f"{_tag(request.method, path, provider, model_name)} 放弃({total_sent}发) {time.time() - start:.1f}s")
            return Response(f'{{"error":{{"message":"upstream overloaded after {total_sent} attempts","type":"upstream_error","code":"503"}}}}', status_code=503, media_type="application/json", headers={"X-Forward-Attempts": str(total_sent)})
        headers = filter_headers(response.headers, SKIP_RESPONSE_HEADERS); headers["X-Forward-Attempts"] = str(winner_attempt)
        # 流式响应禁用反向代理缓冲，否则 nginx/群晖反代会攒批 flush 导致远程访问"一顿一顿"
        if "event-stream" in response.headers.get("content-type", "") or response.headers.get("content-length") is None:
            headers["X-Accel-Buffering"] = "no"; headers["Cache-Control"] = "no-cache"
        async def body_gen():
            try:
                async for chunk in response.aiter_bytes(): yield chunk
            except httpx.TransportError as e:
                logger.warning(f"{_tag(request.method, path, provider, model_name)} 流式中断 #{winner_attempt} {e!r} 总{time.time() - start:.2f}s")
            finally:
                await response.aclose()
        return StreamingResponse(body_gen(), status_code=response.status_code, headers=headers, media_type=response.headers.get("content-type"))
    return health, stats_page, stats_api, logs_page, logs_history, logs_stream, proxy
