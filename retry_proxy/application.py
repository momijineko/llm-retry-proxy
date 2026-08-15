import asyncio
import html
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .access_control import IPBlocklistMiddleware, resolve_client_ip
from .admin_session import create_session as create_admin_session
from .admin_session import revoke as revoke_admin_session
from .api import _with_settings_nav, create_handlers
from .pool_sync import PoolSyncManager
from .sync_adapters import PoolSyncError
from .config import HOT_PARSERS, log_capture, logger, require_admin, settings
from .dlp import load_policy
from .env_file import load_env_file, update_env_file
from .key_pool import KEY_POOLS
from .log_store import RetryLogStore
from .retry import RetryProxy
from .routes import ROUTES, route_registry
from .settings_meta import CONFIG_ITEMS, CONFIG_ITEMS_BY_KEY, GROUPS, HOT, REBUILD

from .sse2ws import create_sse2ws_handler

if sys.platform == "win32":
    os.system("")

store = RetryLogStore()
client = None
pool_sync = PoolSyncManager(KEY_POOLS, route_registry=route_registry)


# 启动横幅（LLM RETRY PROXY ASCII art），打印在启动日志最前
_STARTUP_BANNER = "\n".join(line.rstrip() for line in (
    "██╗     ██╗     ███╗   ███╗      ██████╗ ███████╗████████╗██████╗ ██╗   ██╗     ██████╗ ██████╗  ██████╗ ██╗  ██╗██╗   ██╗",
    "██║     ██║     ████╗ ████║      ██╔══██╗██╔════╝╚══██╔══╝██╔══██╗╚██╗ ██╔╝     ██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝╚██╗ ██╔╝",
    "██║     ██║     ██╔████╔██║█████╗██████╔╝█████╗     ██║   ██████╔╝ ╚████╔╝█████╗██████╔╝██████╔╝██║   ██║ ╚███╔╝  ╚████╔╝",
    "██║     ██║     ██║╚██╔╝██║╚════╝██╔══██╗██╔══╝     ██║   ██╔══██╗  ╚██╔╝ ╚════╝██╔═══╝ ██╔══██╗██║   ██║ ██╔██╗   ╚██╔╝",
    "███████╗███████╗██║ ╚═╝ ██║      ██║  ██║███████╗   ██║   ██║  ██║   ██║        ██║     ██║  ██║╚██████╔╝██╔╝ ██╗   ██║",
    "╚══════╝╚══════╝╚═╝     ╚═╝      ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝   ╚═╝        ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝",
))


def _log_startup():
    # 横幅逐行独立输出：部分日志查看器（面板/HTML 页面）会折叠单条记录内嵌的换行
    for line in _STARTUP_BANNER.splitlines():
        logger.info(f"\033[36m{line}\033[0m")
    logger.info("=" * 60)
    logger.info(f"转发服务启动: http://{settings.listen_host}:{settings.listen_port}")
    for prefix, upstream_url, provider, _ in ROUTES:
        pool_tag = " 号池" if upstream_url in KEY_POOLS else ""
        if prefix:
            logger.info(f"  路由: {prefix}/* -> {upstream_url}/  (provider={provider}, 去前缀{pool_tag})")
        else:
            logger.info(f"  路由: /* -> {upstream_url}  (provider={provider}, 默认{pool_tag})")
    retry_desc = "无限" if settings.max_retries <= 0 else str(settings.max_retries)
    mode_desc = {"off": "串行重试", "race": "请求竞速(一次并发)", "stagger": "滚动竞速(交错补发)"}
    backoff_429 = f"指数退避(上限{settings.retry_backoff_max_429:.0f}s)" if settings.retry_backoff_429 else "固定间隔"
    backoff = f"指数退避(上限{settings.retry_backoff_max:.0f}s)" if settings.retry_backoff else "固定间隔"
    logger.info(f"重试: 间隔={settings.retry_interval}s+{backoff}, 429={settings.retry_interval_429}s+{backoff_429}(优先Retry-After), 最大次数={retry_desc}, 状态码={sorted(settings.retry_status_codes)}, 宽松={'开(5xx/429/401/403)' if settings.retry_broad else '关'}")
    default_mode = mode_desc.get(settings.hedge_mode, settings.hedge_mode)
    concurrency = f", 最大并发={settings.max_concurrent}" if settings.hedge_mode != "off" else ""
    logger.info(f"模式: 普通请求={default_mode}, 号池请求=串行重试{concurrency}")
    logger.info(f"记录: provider={settings.provider}, 日志目录={settings.log_dir}, 保留{settings.log_retention_days}天")
    logger.info(f"DLP: 模式={settings.dlp_mode}, 规则={','.join(sorted(settings.dlp_rules)) if settings.dlp_rules else '无'}")
    logger.info(f"代理: trust_env={'是(跟随系统代理)' if settings.trust_env else '否(直连)'}")
    logger.info(f"管理端鉴权: {'已启用' if settings.admin_password else '未配置（统计与日志端点已禁用）'}")
    logger.info(f"号池访问鉴权: {'已启用' if settings.proxy_api_key else '未配置（兼容开放模式）'}")
    logger.info(f"API文档: {'已启用' if settings.api_docs_enabled else '未启用'}")
    logger.info(
        f"IP黑名单: {len(settings.ip_blacklist)}条, "
        f"可信代理: {len(settings.trusted_proxy_ips)}条"
    )
    auto_ban = (
        "关" if settings.ip_auto_ban_threshold <= 0 else
        f"{settings.ip_auto_ban_window:g}s内{settings.ip_auto_ban_threshold}个不同路径"
        + (" -> 永久封禁" if settings.ip_auto_ban_duration == 0 else
           f" -> 封禁{settings.ip_auto_ban_duration:g}s")
    )
    logger.info(f"IP动态封禁: {auto_ban}")
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
    if settings.sse2ws_mode not in ("off", "bridge", "on", "1", "true"):
        raise ValueError(f"未知 SSE2WS_MODE: {settings.sse2ws_mode!r}")
    if settings.sse2ws_mode != "off" and settings.sse2ws_first_event_timeout <= 0:
        raise ValueError("SSE2WS_FIRST_EVENT_TIMEOUT 必须大于 0")
    if settings.max_request_body <= 0:
        raise ValueError("MAX_REQUEST_BODY 必须大于 0")
    if settings.ip_auto_ban_threshold < 0:
        raise ValueError("IP_AUTO_BAN_THRESHOLD 不能小于 0")
    if settings.ip_auto_ban_threshold and settings.ip_auto_ban_window <= 0:
        raise ValueError("IP_AUTO_BAN_WINDOW 必须大于 0")
    if settings.ip_auto_ban_duration < 0:
        raise ValueError("IP_AUTO_BAN_DURATION 不能小于 0")
    if settings.key_cache_miss_threshold < 0:
        raise ValueError("KEY_CACHE_MISS_THRESHOLD 不能小于 0")
    if settings.key_cache_miss_min_input_tokens < 0:
        raise ValueError("KEY_CACHE_MISS_MIN_INPUT_TOKENS 不能小于 0")
    if settings.key_cache_miss_cooldown < 0:
        raise ValueError("KEY_CACHE_MISS_COOLDOWN 不能小于 0")
    if settings.dlp_mode != "off":
        policy = load_policy(settings.dlp_rule_file)
        unknown_rules = settings.dlp_rules - (policy.rules.keys() | {"structured_secret"})
        if unknown_rules:
            raise ValueError(f"DLP_RULES 包含未知规则: {','.join(sorted(unknown_rules))}")
        if (settings.dlp_allow_exemptions
                and (not settings.dlp_exempt_start or not settings.dlp_exempt_end
                     or settings.dlp_exempt_start == settings.dlp_exempt_end)):
            raise ValueError("DLP 豁免起止标记不能为空或相同")
        if settings.dlp_max_body_bytes <= 0:
            raise ValueError("DLP_MAX_BODY_BYTES 必须大于 0")
        if settings.dlp_decode_depth < 0 or settings.dlp_decode_depth > 8:
            raise ValueError("DLP_DECODE_DEPTH 必须在 0 到 8 之间")
        if settings.dlp_decode_depth and settings.dlp_decode_max_candidates <= 0:
            raise ValueError("DLP_DECODE_MAX_CANDIDATES 必须大于 0")
        if settings.dlp_decode_depth and settings.dlp_decode_max_bytes <= 0:
            raise ValueError("DLP_DECODE_MAX_BYTES 必须大于 0")
        if settings.dlp_known_secret_min_length <= 0:
            raise ValueError("DLP_KNOWN_SECRET_MIN_LENGTH 必须大于 0")
    store.initialize()
    pool_sync.load_state()
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout, connect=settings.connect_timeout),
                               limits=httpx.Limits(max_connections=200, max_keepalive_connections=50), trust_env=settings.trust_env)
    # 同步适配器使用独立的客户端，避免长耗时 create_keys 等操作占用代理转发的连接池。
    sync_client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=settings.connect_timeout),
                                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10), trust_env=settings.trust_env)
    service.client = client
    pool_sync.client = sync_client
    app.state.retry_proxy = service
    app.state.pool_sync = pool_sync
    log_capture.set_loop(asyncio.get_event_loop())
    _log_startup()
    await pool_sync.start()
    try:
        yield
    finally:
        await pool_sync.stop()
        await sync_client.aclose()
        await client.aclose()
        store.flush()


def _api_docs_options(enabled):
    return {
        "docs_url": "/docs" if enabled else None,
        "redoc_url": "/redoc" if enabled else None,
        "openapi_url": "/openapi.json" if enabled else None,
        "swagger_ui_oauth2_redirect_url": "/docs/oauth2-redirect" if enabled else None,
    }


def _register_disabled_api_docs(target_app):
    async def api_docs_disabled():
        return HTMLResponse("api docs disabled", status_code=404)

    # 精确拦截，避免禁用 FastAPI 内置路由后这些路径落入代理 catch-all。
    for path in (
        "/docs", "/docs/", "/docs/oauth2-redirect",
        "/redoc", "/redoc/", "/openapi.json", "/openapi.json/",
    ):
        target_app.add_api_route(
            path,
            api_docs_disabled,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )


app = FastAPI(
    title="llm-retry-proxy",
    lifespan=lifespan,
    **_api_docs_options(settings.api_docs_enabled),
)
if not settings.api_docs_enabled:
    _register_disabled_api_docs(app)
app.add_middleware(
    IPBlocklistMiddleware,
    blacklist=settings.ip_blacklist,
    trusted_proxies=settings.trusted_proxy_ips,
    auto_ban_threshold=settings.ip_auto_ban_threshold,
    auto_ban_window=settings.ip_auto_ban_window,
    auto_ban_duration=settings.ip_auto_ban_duration,
    auto_ban_exempt=settings.ip_auto_ban_exempt,
    state_file=settings.ip_ban_state_file,
)
service = RetryProxy(client=None, pools=KEY_POOLS, log_store=store)
health, stats_page, stats_api, logs_page, logs_history, logs_stream, proxy = create_handlers(
    service, store, pool_sync,
)


# 登录限速状态（进程内）：按客户端 IP 记录连续失败次数与锁定到期时间。
# 与 IP 动态封禁互补：后者按"窗口内访问不同路径"计数，无法覆盖对
# 单一 /admin/login 路径的暴力尝试。
_LOGIN_STATE_MAX = 10000
_login_attempts: dict = {}  # ip -> {"failures","locked_until","multiplier","last_seen"}


def _login_attempt_state(ip, now):
    state = _login_attempts.get(ip)
    if state is None:
        if len(_login_attempts) >= _LOGIN_STATE_MAX:
            stale = [
                key for key, value in _login_attempts.items()
                if value["locked_until"] <= now and now - value["last_seen"] > 3600
            ]
            for key in stale:
                _login_attempts.pop(key, None)
            if len(_login_attempts) >= _LOGIN_STATE_MAX:
                oldest = min(_login_attempts, key=lambda key: _login_attempts[key]["last_seen"])
                _login_attempts.pop(oldest, None)
        state = {"failures": 0, "locked_until": 0.0, "multiplier": 1, "last_seen": now}
        _login_attempts[ip] = state
    state["last_seen"] = now
    return state


def _login_locked(ip, now):
    """返回剩余锁定秒数；未锁定返回 0。ADMIN_LOGIN_MAX_ATTEMPTS<=0 时恒不锁定。"""
    if settings.admin_login_max_attempts <= 0:
        return 0.0
    state = _login_attempt_state(ip, now)
    remaining = state["locked_until"] - now
    if remaining <= 0:
        state["locked_until"] = 0.0
        return 0.0
    return remaining


def _login_failure(ip, now):
    """记录一次登录失败；达到阈值时触发锁定，锁定时长逐次翻倍（上限 8 倍）。"""
    if settings.admin_login_max_attempts <= 0:
        return
    state = _login_attempt_state(ip, now)
    state["failures"] += 1
    if state["failures"] >= settings.admin_login_max_attempts:
        lockout = max(settings.admin_login_lockout_seconds, 0.0) * state["multiplier"]
        state["multiplier"] = min(state["multiplier"] * 2, 8)
        state["failures"] = 0
        state["locked_until"] = max(now + lockout, state["locked_until"])


def _login_success(ip):
    _login_attempts.pop(ip, None)


def _login_page(next_path="/stats", failed=False, notice="", status_code=200):
    next_path = next_path if next_path in ("/stats", "/logs", "/key-pools", "/settings") else "/stats"
    error = '<p class="error">密码不正确</p>' if failed else ""
    notice_block = f'<p class="error">{html.escape(notice)}</p>' if notice else ""
    disabled = "" if settings.admin_password else "disabled"
    message = "" if settings.admin_password else '<p class="error">管理员密码尚未配置</p>'
    return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>管理端登录</title>
<style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f8;color:#172033;font:14px system-ui,sans-serif}}main{{width:min(360px,calc(100% - 32px));background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:28px;box-shadow:0 8px 30px rgba(15,23,42,.08)}}h1{{margin:0 0 22px;font-size:20px;letter-spacing:0}}label{{display:block;margin-bottom:7px;color:#526071;font-size:12px}}input{{width:100%;height:42px;border:1px solid #cbd3dc;border-radius:6px;padding:0 12px;font:inherit;outline:none}}input:focus{{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}}button{{width:100%;height:42px;margin-top:16px;border:0;border-radius:6px;background:#2563eb;color:#fff;font:600 14px system-ui;cursor:pointer}}button:disabled{{background:#9ca3af;cursor:not-allowed}}.error{{margin:0 0 14px;color:#c2413b;font-size:12px}}</style></head><body><main><h1>管理端登录</h1>{message}{notice_block}{error}<form method="post" action="/admin/login"><input type="hidden" name="next" value="{html.escape(next_path)}"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required {disabled}><button type="submit" {disabled}>登录</button></form></main></body></html>""", status_code=status_code)


async def admin_login_page(next: str = "/stats"):
    return _login_page(next)


async def admin_login(request: Request):
    values = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    password = values.get("password", [""])[0]
    next_path = values.get("next", ["/stats"])[0]
    next_path = next_path if next_path in ("/stats", "/logs", "/key-pools", "/settings") else "/stats"
    client_ip = resolve_client_ip(request.scope, settings.trusted_proxy_ips)
    now = time.monotonic()
    locked_for = _login_locked(client_ip, now)
    if locked_for > 0:
        logger.warning(f"[{client_ip}] 管理端登录被限速锁定 {locked_for:.0f}s")
        return _login_page(
            next_path,
            notice=f"尝试次数过多，请 {math.ceil(locked_for)} 秒后再试",
            status_code=429,
        )
    if not settings.admin_password or not secrets.compare_digest(password, settings.admin_password):
        _login_failure(client_ip, now)
        return _login_page(next_path, failed=True)
    _login_success(client_ip)
    token = create_admin_session()
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie("admin_session", token, max_age=30 * 86400,
                        httponly=True, samesite="strict", secure=settings.admin_cookie_secure, path="/")
    return response


async def admin_logout(request: Request):
    session = request.cookies.get("admin_session", "")
    if session:
        revoke_admin_session(session)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("admin_session", path="/")
    return response


async def key_pools_page():
    path = settings.key_pool_html_path
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(_with_settings_nav(f.read()))
    return HTMLResponse("key_pool.html not found", status_code=404)


# 配置中心写入的目标 .env 文件；容器模式未挂载时仅热应用不持久化
_ENV_FILE_PATH = ".env"


async def settings_page():
    path = settings.settings_html_path
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("settings.html not found", status_code=404)


def _fmt_ip_network(net):
    """ipaddress 网络对象转可写回 .env 的纯文本：去掉 /32、/128 全掩码后缀"""
    s = str(net)
    mx = getattr(net, "max_prefixlen", None)
    if mx is not None:
        suffix = f"/{mx}"
        if s.endswith(suffix):
            return s[:-len(suffix)]
    return s


def _effective_value(item) -> str:
    """返回配置项的当前生效值（优先 Settings 属性，无属性时读环境变量）"""
    attr = item.attr or item.key.lower()
    if not hasattr(settings, attr):
        # 未写入环境变量时以元数据默认值为准，避免页面把生效值误显示为空
        value = os.getenv(item.key, item.default)
    else:
        value = getattr(settings, attr)
    if isinstance(value, frozenset):
        return ",".join(str(v) for v in sorted(value))
    if isinstance(value, tuple):
        # IP/CIDR 元组（parse_ip_networks 产物）：还原为逗号分隔纯文本，
        # 避免 Python repr（如 IPv4Network('...')）写入 .env 导致重启解析失败
        return ",".join(_fmt_ip_network(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def settings_get():
    file_values = load_env_file(_ENV_FILE_PATH)
    items = []
    for item in CONFIG_ITEMS:
        effective = _effective_value(item)
        entry = {
            "key": item.key,
            "name": item.name or item.key,
            "group": item.group,
            "type": item.type,
            "enum": list(item.enum),
            "default": item.default,
            "secret": item.secret,
            "apply": item.apply,
            "hidden": item.hidden,
            "unit": item.unit,
            "min": item.min,
            "max": item.max,
            "description": item.description,
        }
        if item.secret:
            # 敏感项不回传 .env 明文，仅返回是否已配置
            entry["configured"] = bool(effective)
        else:
            entry["effective_value"] = effective
            entry["file_value"] = file_values.get(item.key, "")
        items.append(entry)
    return {"items": items, "groups": list(GROUPS), "persisted": os.path.exists(_ENV_FILE_PATH)}


_BOOL_ACCEPTED = ("1", "true", "yes", "on", "0", "false", "no", "off")


def _check_range(item, number):
    """按元数据登记的 min/max（闭区间）校验数值，非法值返回 400"""
    if item.min is not None and number < item.min:
        raise HTTPException(status_code=400, detail=f"{item.key} 不能小于 {item.min:g}")
    if item.max is not None and number > item.max:
        raise HTTPException(status_code=400, detail=f"{item.key} 不能大于 {item.max:g}")


def _validate_value(item, raw) -> str:
    """按元数据类型校验并规范化配置值，非法输入返回 400"""
    text = str(raw).strip()
    if text.endswith("\\"):
        # python-dotenv 1.2.2 对双引号值以反斜杠结尾的解析不稳定（相邻同引号行时
        # 整个文件被丢弃），保存时直接拒绝以避免重启后配置静默丢失
        raise HTTPException(status_code=400,
                            detail=f"{item.key} 不能以反斜杠结尾（python-dotenv 兼容性限制）")
    if len(text.splitlines()) > 1:
        # 换行（含 \v \f \u2028 等通用换行符）会破坏 .env 的单行结构，写入后无法被行式解析还原
        raise HTTPException(status_code=400,
                            detail=f"{item.key} 不能包含换行符")
    if item.type == "bool":
        if text.lower() not in _BOOL_ACCEPTED:
            raise HTTPException(status_code=400, detail=f"{item.key} 必须是布尔值")
        return "true" if text.lower() in ("1", "true", "yes", "on") else "false"
    if item.type == "int":
        try:
            number = int(text)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{item.key} 必须是整数")
        _check_range(item, number)
        return text
    if item.type == "float":
        try:
            number = float(text)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{item.key} 必须是数字")
        if not math.isfinite(number):
            raise HTTPException(status_code=400, detail=f"{item.key} 必须是有限数字")
        _check_range(item, number)
        return text
    if item.type == "enum":
        if text not in item.enum and text.lower() not in {e.lower() for e in item.enum}:
            raise HTTPException(status_code=400,
                                detail=f"{item.key} 可选值: {', '.join(item.enum)}")
        return text
    if item.type == "csv":
        # csv 项均为热更新键，用登记解析器试解析以拦截非法值（如非整数状态码）
        parser = HOT_PARSERS.get(item.key)
        if parser is not None:
            try:
                parser[1](text)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400,
                                    detail=f"{item.key} 必须是逗号分隔的有效值")
        return text
    return text


def _redact_secrets(values: dict) -> dict:
    """日志脱敏：secret 配置项的值以掩码输出，避免敏感凭据写入日志"""
    return {key: ("***" if CONFIG_ITEMS_BY_KEY[key].secret else value)
            for key, value in values.items()}


async def settings_post(request: Request):
    body = await _json_object(request, allow_empty=True)
    updates = body.get("updates")
    remove = body.get("remove")
    if updates is None:
        updates = {}
    if remove is None:
        remove = []
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="updates 必须是对象")
    if not isinstance(remove, list) or not all(isinstance(k, str) for k in remove):
        raise HTTPException(status_code=400, detail="remove 必须是字符串数组")
    remove = set(remove)

    normalized = {}
    removals = set(remove)
    hot_apply = {}
    restart = []
    rebuild = []
    # remove 数组（页面"重置/清空"路径）：从 .env 删除该键；热更新项立即恢复默认值
    for key in remove:
        item = CONFIG_ITEMS_BY_KEY.get(key)
        if item is None:
            raise HTTPException(status_code=400, detail=f"未知配置项: {key}")
        if item.apply == REBUILD:
            rebuild.append(key)
        elif item.apply == HOT:
            hot_apply[key] = item.default
        else:
            restart.append(key)
    # updates 优先于 remove：同一键写入新值时以 updates 为准（hot_apply 后写覆盖）
    for key, raw in updates.items():
        item = CONFIG_ITEMS_BY_KEY.get(key)
        if item is None:
            raise HTTPException(status_code=400, detail=f"未知配置项: {key}")
        if str(raw).strip() == "":
            # 空值语义 = 恢复默认：从 .env 删除该键，热更新项直接应用默认值
            removals.add(key)
            if item.apply == REBUILD:
                if key not in rebuild: rebuild.append(key)
            elif item.apply == HOT:
                hot_apply[key] = item.default
            elif key not in restart:
                restart.append(key)
            continue
        value = _validate_value(item, raw)
        normalized[key] = value
        if item.apply == REBUILD:
            if key not in rebuild: rebuild.append(key)
        elif item.apply == HOT:
            hot_apply[key] = value
        elif key not in restart:
            restart.append(key)

    # 同一键同时出现时 updates 优先，响应和日志也不应再把它报告为已移除。
    removals.difference_update(normalized)
    persisted = False
    if normalized or removals:
        persisted = update_env_file(_ENV_FILE_PATH, normalized, removals)
    applied = settings.apply_env(hot_apply)
    logger.info(f"配置中心保存: 写入={_redact_secrets(normalized)} 移除={sorted(removals)} 热应用={applied} 持久化={persisted}")
    return {
        "applied": applied,
        "need_restart": restart,
        "need_rebuild": rebuild,
        "removed": sorted(removals),
        "persisted": persisted,
    }


async def legacy_key_pools_page():
    return RedirectResponse("/key-pools", status_code=308)


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
            body.get("route_prefix"),
        )
    except (ValueError, TypeError, PoolSyncError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_sync(request: Request):
    try:
        body = await _json_object(request, allow_empty=True)
        return await pool_sync.sync_now(body.get("source_id"))
    except PoolSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def key_pools_delete(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.delete(body.get("source_id"))
    except (ValueError, TypeError, PoolSyncError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


async def key_pools_experience_source(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.set_experience_source(
            body.get("source_id"), body.get("url"), body.get("samples", 100),
            body.get("sample_param", "samples"), body.get("transform"),
            body.get("query_params") if "query_params" in body else None,
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_experience_mapping(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.set_experience_mapping(
            body.get("source_id"), body.get("mappings") or {},
        )
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


async def key_pools_source_settings(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.set_source_settings(
            body.get("source_id"), body.get("strategy"), body.get("target_ttft_s", 5.0),
            body.get("check_model", ""), body.get("session_affinity"),
            body.get("external_retest_weight"),
            body.get("external_ttft_prior_strength"),
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_check(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.check_availability(
            body.get("source_id"), body.get("model"),
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_reset_group(request: Request):
    try:
        body = await _json_object(request)
        group_id = body.get("group_id")
        if group_id in (None, ""):
            raise HTTPException(status_code=400, detail="group_id 不能为空")
        return await pool_sync.reset_group(body.get("source_id"), str(group_id))
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_reset_groups(request: Request):
    try:
        body = await _json_object(request)
        return await pool_sync.reset_groups(body.get("source_id"))
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


async def key_pools_key_state(request: Request):
    try:
        body = await _json_object(request)
        source_key_id = body.get("source_key_id")
        if source_key_id in (None, ""):
            raise HTTPException(status_code=400, detail="source_key_id 不能为空")
        return await pool_sync.set_key_enabled(
            body.get("source_id"), source_key_id, body.get("enabled"),
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_manual_add(request: Request):
    try:
        body = await _json_object(request)
        raw_base_url = body.get("base_url")
        if not isinstance(raw_base_url, str):
            raise HTTPException(status_code=400, detail="base_url 必须是字符串")
        base_url = raw_base_url.strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="base_url 不能为空")
        keys = body.get("keys")
        if not isinstance(keys, list) or not keys:
            raise HTTPException(status_code=400, detail="keys 必须是非空数组")
        return await pool_sync.add_manual_keys(
            base_url, keys,
            provider=body.get("provider", ""),
            route_prefix=body.get("route_prefix"),
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_manual_remove(request: Request):
    try:
        body = await _json_object(request)
        source_id = body.get("source_id")
        if not source_id:
            raise HTTPException(status_code=400, detail="source_id 不能为空")
        source_key_ids = body.get("source_key_ids")
        if not isinstance(source_key_ids, list) or not source_key_ids:
            raise HTTPException(status_code=400, detail="source_key_ids 必须是非空数组")
        return await pool_sync.remove_manual_keys(source_id, source_key_ids)
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def key_pools_manual_update(request: Request):
    try:
        body = await _json_object(request)
        source_id = body.get("source_id")
        source_key_id = body.get("source_key_id")
        if not source_id:
            raise HTTPException(status_code=400, detail="source_id 不能为空")
        if source_key_id in (None, ""):
            raise HTTPException(status_code=400, detail="source_key_id 不能为空")
        return await pool_sync.update_manual_key(
            source_id, source_key_id, body,
        )
    except PoolSyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


app.add_api_route("/health", health, methods=["GET"])
app.add_api_route("/admin/login", admin_login_page, methods=["GET"])
app.add_api_route("/admin/login", admin_login, methods=["POST"])
app.add_api_route("/admin/logout", admin_logout, methods=["POST"])
admin_dependencies = [Depends(require_admin)]
app.add_api_route("/key-pools", key_pools_page, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools", legacy_key_pools_page, methods=["GET"])
app.add_api_route("/admin/key-pools/api/status", key_pools_status, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/connect", key_pools_connect, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/sync", key_pools_sync, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/delete", key_pools_delete, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/disconnect", key_pools_disconnect, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/catalog", key_pools_catalog, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/create-keys", key_pools_create_keys, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/group-rules", key_pools_group_rules, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/experience-source", key_pools_experience_source, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/experience-mapping", key_pools_experience_mapping, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/clear-keys", key_pools_clear_keys, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/settings", key_pools_settings, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/source-settings", key_pools_source_settings, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/check", key_pools_check, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/reset-group", key_pools_reset_group, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/reset-groups", key_pools_reset_groups, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/reset-key", key_pools_reset_key, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/key-state", key_pools_key_state, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/manual-add", key_pools_manual_add, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/manual-remove", key_pools_manual_remove, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/admin/key-pools/api/manual-update", key_pools_manual_update, methods=["POST"], dependencies=admin_dependencies)
app.add_api_route("/stats", stats_page, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/stats/api", stats_api, methods=["GET"], dependencies=admin_dependencies)
if settings.settings_page_enabled:
    app.add_api_route("/settings", settings_page, methods=["GET"], dependencies=admin_dependencies)
    app.add_api_route("/admin/settings", settings_get, methods=["GET"], dependencies=admin_dependencies)
    app.add_api_route("/admin/settings", settings_post, methods=["POST"], dependencies=admin_dependencies)
    logger.info("配置中心页面: 已启用 (/settings)")
else:
    # 关闭时不注册管理路由，避免落入 /{path:path} catch-all 被透传上游；404 不带鉴权
    async def settings_disabled():
        return HTMLResponse("settings page disabled", status_code=404)

    app.add_api_route("/settings", settings_disabled, methods=["GET"])
    app.add_api_route("/admin/settings", settings_disabled, methods=["GET", "POST"])
    logger.info("配置中心页面: 未启用(SETTINGS_PAGE_ENABLED=true 开启)")
app.add_api_route("/logs", logs_page, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/logs/history", logs_history, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/logs/stream", logs_stream, methods=["GET"], dependencies=admin_dependencies)
app.add_api_route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
if settings.sse2ws_mode in ("bridge", "on", "1", "true"):
    sse2ws_handler = create_sse2ws_handler(service, store)
    app.add_api_websocket_route("/{path:path}", sse2ws_handler)
    logger.info(f"SSE2WS 桥接: 已启用 (首事件超时={settings.sse2ws_first_event_timeout:.0f}s "
                f"重试={settings.sse2ws_first_event_retries}次 会话空闲={settings.sse2ws_inter_turn_idle_timeout:.0f}s)")
else:
    logger.info("SSE2WS 桥接: 未启用(SSE2WS_MODE=bridge 开启 Codex Responses WebSocket 桥接)")
