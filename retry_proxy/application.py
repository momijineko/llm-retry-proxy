import asyncio
import os
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .api import create_handlers
from .config import log_capture, logger, settings
from .dlp import load_policy
from .key_pool import KEY_POOLS
from .log_store import RetryLogStore
from .retry import RetryProxy
from .routes import ROUTES

if sys.platform == "win32":
    os.system("")

store = RetryLogStore()
client = None


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
    if KEY_POOLS:
        for pool_url, pool in KEY_POOLS.items():
            route_tag = "默认" if pool_url == settings.upstream_url else pool_url
            labels = ", ".join(e.key_id for e in pool.entries)
            logger.info(f"号池: {route_tag} provider={pool.provider or settings.provider} keys={len(pool.entries)}个 冷却={settings.key_cooldown:.0f}s 粘性={settings.key_sticky:.0f}s 鉴权={settings.key_auth_header}({'有' if settings.key_auth_scheme else '无'}scheme)")
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
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout, connect=settings.connect_timeout),
                               limits=httpx.Limits(max_connections=200, max_keepalive_connections=50), trust_env=settings.trust_env)
    service.client = client
    app.state.retry_proxy = service
    log_capture.set_loop(asyncio.get_event_loop())
    _log_startup()
    try:
        yield
    finally:
        await client.aclose()
        if store.summary_cache:
            store._save()


app = FastAPI(title="llm-retry-proxy", lifespan=lifespan)
service = RetryProxy(client=None, pools=KEY_POOLS, log_store=store)
health, stats_page, stats_api, logs_page, logs_history, logs_stream, proxy = create_handlers(service, store)
app.add_api_route("/health", health, methods=["GET"])
app.add_api_route("/stats", stats_page, methods=["GET"])
app.add_api_route("/stats/api", stats_api, methods=["GET"])
app.add_api_route("/logs", logs_page, methods=["GET"])
app.add_api_route("/logs/history", logs_history, methods=["GET"])
app.add_api_route("/logs/stream", logs_stream, methods=["GET"])
app.add_api_route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
