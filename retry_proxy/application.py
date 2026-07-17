import asyncio
import html
import os
import secrets
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .api import create_handlers
from .pool_sync import PoolSyncManager
from .sync_adapters import PoolSyncError
from .config import admin_session_value, log_capture, logger, require_admin, settings
from .dlp import load_policy
from .key_pool import KEY_POOLS
from .log_store import RetryLogStore
from .retry import RetryProxy
from .routes import ROUTES

if sys.platform == "win32":
    os.system("")

store = RetryLogStore()
client = None
pool_sync = PoolSyncManager(KEY_POOLS)


def _log_startup():
    logger.info("=" * 60)
    logger.info(f"转发服务启动: http://{settings.listen_host}:{settings.listen_port}")
    for prefix, upstream_url, provider, _ in ROUTES:
        pool_tag = " 号池" if upstream_url in KEY_POOLS else ""
        if prefix:
            logger.info(f"  路由: {prefix}/* -> {upstream_url}/  (provider={provider}, 去前缀{pool_tag})")
        else:
            logger.info(f"  路由: /* -> {upstream_url}  (provider={provider}, 默认{pool_tag})")
    retry_desc = "无限" if settings.max_retries <= 0 else str(settings.max_retries)
    mode_desc = {"off": "串行重试", "race": "请求竞速(一次并发)", "stagger": "滚动竞速(交错补发)"}.get(settings.hedge_mode, settings.hedge_mode)
    backoff_429 = f"指数退避(上限{settings.retry_backoff_max_429:.0f}s)" if settings.retry_backoff_429 else "固定间隔"
    backoff = f"指数退避(上限{settings.retry_backoff_max:.0f}s)" if settings.retry_backoff else "固定间隔"
    logger.info(f"重试: 间隔={settings.retry_interval}s+{backoff}, 429={settings.retry_interval_429}s+{backoff_429}(优先Retry-After), 最大次数={retry_desc}, 状态码={sorted(settings.retry_status_codes)}, 宽松={'开(5xx/429/401/403)' if settings.retry_broad else '关'}")
    logger.info(f"模式: {mode_desc}" + (f", 最大并发={settings.max_concurrent}" if settings.hedge_mode != "off" else ""))
    logger.info(f"记录: provider={settings.provider}, 日志目录={settings.log_dir}, 保留{settings.log_retention_days}天")
    logger.info(f"DLP: 模式={settings.dlp_mode}, 规则={','.join(sorted(settings.dlp_rules)) if settings.dlp_rules else '无'}")
    logger.info(f"代理: trust_env={'是(跟随系统代理)' if settings.trust_env else '否(直连)'}")
    logger.info(f"管理端鉴权: {'已启用' if settings.admin_password else '未配置（统计与日志端点已禁用）'}")
    logger.info(f"号池访问鉴权: {'已启用' if settings.proxy_api_key else '未配置（兼容开放模式）'}")
    if KEY_POOLS:
        for pool_url, pool in KEY_POOLS.items():
            route_tag = "默认" if pool_url == settings.upstream_url else pool_url
            labels = ", ".join(e.key_id for e in pool.entries)
            cooldown_desc = (f"5xx={settings.key_cooldown_5xx:.0f}s/429={settings.key_cooldown_429:.0f}s/"
                             f"鉴权={settings.key_cooldown_auth:.0f}s/上限={settings.key_cooldown_max:.0f}s/"
                             f"指数={'开' if settings.key_cooldown_backoff else '关'}")
            logger.info(f"号池: {route_tag} provider={pool.provider or settings.provider} keys={len(pool.entries)}个 熔断={cooldown_desc} 粘性={settings.key_sticky:.0f}s 鉴权头={settings.key_auth_header}({'有' if settings.key_auth_scheme else '无'}scheme)")
    else:
        logger.info("号池: 未配置(透传客户端key)")
    logger.info(f"统计面板: http://127.0.0.1:{settings.listen_port}/stats")
    logger.info(f"日志面板: http://127.0.0.1:{settings.listen_port}/logs")
    logger.info("=" * 60)


@asynccontextmanager
async def lifespan(_app):
    global client
    if settings.dlp_mode not in ("off", "audit", "redact", "block"):
        raise ValueError(f"未知 DLP_MODE: {settings.dlp_mode!r}")
    if settings.dlp_mode != "off":
        policy = load_policy(settings.dlp_rule_file)
        unknown_rules = settings.dlp_rules - (policy.rules.keys() | {"structured_secret"})
        if unknown_rules:
            raise ValueError(f"DLP_RULES 包含未知规则: {','.join(sorted(unknown_rules))}")
        if (not settings.dlp_exempt_start or not settings.dlp_exempt_end
                or settings.dlp_exempt_start == settings.dlp_exempt_end):
            raise ValueError("DLP 豁免起止标记不能为空或相同")
        if settings.dlp_max_body_bytes <= 0:
            raise ValueError("DLP_MAX_BODY_BYTES 必须大于 0")
    store.initialize()
    pool_sync.load_state()
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout, connect=settings.connect_timeout),
                               limits=httpx.Limits(max_connections=200, max_keepalive_connections=50), trust_env=settings.trust_env)
    service.client = client
    pool_sync.client = client
    app.state.retry_proxy = service
    app.state.pool_sync = pool_sync
    log_capture.set_loop(asyncio.get_event_loop())
    _log_startup()
    await pool_sync.start()
    try:
        yield
    finally:
        await pool_sync.stop()
        await client.aclose()
        if store.summary_cache:
            store._save()


app = FastAPI(title="llm-retry-proxy", lifespan=lifespan)
service = RetryProxy(client=None, pools=KEY_POOLS, log_store=store)
health, stats_page, stats_api, logs_page, logs_history, logs_stream, proxy = create_handlers(service, store)


def _login_page(next_path="/stats", failed=False):
    next_path = next_path if next_path in ("/stats", "/logs", "/admin/key-pools") else "/stats"
    error = '<p class="error">密码不正确</p>' if failed else ""
    disabled = "" if settings.admin_password else "disabled"
    message = "" if settings.admin_password else '<p class="error">管理员密码尚未配置</p>'
    return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>管理端登录</title>
<style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f8;color:#172033;font:14px system-ui,sans-serif}}main{{width:min(360px,calc(100% - 32px));background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:28px;box-shadow:0 8px 30px rgba(15,23,42,.08)}}h1{{margin:0 0 22px;font-size:20px;letter-spacing:0}}label{{display:block;margin-bottom:7px;color:#526071;font-size:12px}}input{{width:100%;height:42px;border:1px solid #cbd3dc;border-radius:6px;padding:0 12px;font:inherit;outline:none}}input:focus{{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}}button{{width:100%;height:42px;margin-top:16px;border:0;border-radius:6px;background:#2563eb;color:#fff;font:600 14px system-ui;cursor:pointer}}button:disabled{{background:#9ca3af;cursor:not-allowed}}.error{{margin:0 0 14px;color:#c2413b;font-size:12px}}</style></head><body><main><h1>管理端登录</h1>{message}{error}<form method="post" action="/admin/login"><input type="hidden" name="next" value="{html.escape(next_path)}"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required {disabled}><button type="submit" {disabled}>登录</button></form></main></body></html>""")


async def admin_login_page(next: str = "/stats"):
    return _login_page(next)


async def admin_login(request: Request):
    values = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    password = values.get("password", [""])[0]
    next_path = values.get("next", ["/stats"])[0]
    next_path = next_path if next_path in ("/stats", "/logs", "/admin/key-pools") else "/stats"
    if not settings.admin_password or not secrets.compare_digest(password, settings.admin_password):
        return _login_page(next_path, failed=True)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie("admin_session", admin_session_value(), max_age=30 * 86400,
                        httponly=True, samesite="strict", secure=settings.admin_cookie_secure, path="/")
    return response


async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("admin_session", path="/")
    return response


async def key_pools_page():
    path = settings.key_pool_html_path
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("key_pool.html not found", status_code=404)


async def key_pools_status():
    return pool_sync.status()


async def _json_object(request, allow_empty=False):
    try:
        body = await request.json()
    except ValueError:
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return body


async def key_pools_connect(request: Request):
    try:
        body = await _json_object(request)
        credentials = body.get("credentials") or {
            "email": body.get("email"), "password": body.get("password"),
        }
        return await pool_sync.connect(
            body.get("adapter"), body.get("base_url"), body.get("provider"), credentials,
        )
    except (ValueError, TypeError, PoolSyncError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_sync(request: Request):
    try:
        body = await _json_object(request, allow_empty=True)
        return await pool_sync.sync_now(body.get("source_id"))
    except PoolSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def key_pools_disconnect(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.disconnect(body.get("source_id"))
    except (ValueError, TypeError, PoolSyncError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_catalog(source_id: str):
    try:
        return await pool_sync.catalog(source_id)
    except PoolSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def key_pools_create_keys(request: Request):
    try:
        body = await _json_object(request)
        group_ids = body.get("group_ids") or []
        if not isinstance(group_ids, list):
            raise HTTPException(status_code=400, detail="group_ids 必须是数组")
        return await pool_sync.create_keys(
            body.get("source_id"), group_ids, body.get("only_missing", False),
            {"name_prefix": body.get("name_prefix", "")},
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def key_pools_group_rules(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.set_group_rules(body.get("source_id"), body.get("rules") or {})
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_clear_keys(request: Request):
    try:
        body = await _json_object(request)
        group_ids = body.get("group_ids") or []
        if not isinstance(group_ids, list) or not group_ids:
            raise HTTPException(status_code=400, detail="group_ids 必须是非空数组")
        return await pool_sync.clear_keys(body.get("source_id"), group_ids)
    except PoolSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def key_pools_settings(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.set_interval(body.get("interval"))
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_reset_key(request: Request):
    try:
        body = await _json_object(request)
        source_key_id = body.get("source_key_id")
        if source_key_id in (None, ""):
            raise HTTPException(status_code=400, detail="source_key_id 不能为空")
        return await pool_sync.reset_key(body.get("source_id"), source_key_id)
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


app.add_api_route("/health", health, methods=["GET"])
app.add_api_route("/admin/login", admin_login_page, methods=["GET"])
app.add_api_route("/admin/login", admin_login, methods=["POST"])
app.add_api_route("/admin/logout", admin_logout, methods=["POST"])
admin_dependencies = [Depends(require_admin)]
app.add_api_route("/admin/key-pools", key_pools_page, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/status", key_pools_status, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/connect", key_pools_connect, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/sync", key_pools_sync, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/disconnect", key_pools_disconnect, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/catalog", key_pools_catalog, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/create-keys", key_pools_create_keys, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/group-rules", key_pools_group_rules, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/clear-keys", key_pools_clear_keys, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/settings", key_pools_settings, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/reset-key", key_pools_reset_key, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/stats", stats_page, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/stats/api", stats_api, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/logs", logs_page, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/logs/history", logs_history, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/logs/stream", logs_stream, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
