import asyncio
import collections
import hashlib
import logging
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from io import StringIO

from dotenv import load_dotenv
from fastapi import HTTPException, Request

from .access_control import parse_ip_networks


def safe_load_env(path: str = ".env"):
    if not os.path.exists(path):
        return
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        print(f"[forward] 警告: {path} 编码无法识别(非 UTF-8/GBK)，跳过加载", file=sys.stderr)
        return
    load_dotenv(stream=StringIO(text))


safe_load_env()


class _ColorFmt(logging.Formatter):
    _LV = {"DEBUG": "36", "INFO": "32", "WARNING": "33", "ERROR": "31"}

    def format(self, record):
        t = time.strftime("%m-%d %H:%M:%S", time.localtime(record.created))
        c = self._LV.get(record.levelname, "")
        return f"\033[90m{t}\033[0m \033[{c}m{record.levelname[0]}\033[0m {record.getMessage()}"


_h = logging.StreamHandler()
_h.setFormatter(_ColorFmt())
_console_level = os.getenv("LOG_LEVEL", "INFO").upper()
_h.setLevel(_console_level)
logging.basicConfig(level=_console_level, handlers=[_h])
logger = logging.getLogger("forward")
# Keep request tracing available to the in-memory log page without making the
# console verbose. The console handler still honors LOG_LEVEL.
logger.setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


class LogCaptureHandler(logging.Handler):
    """Thread-safe log handler: ring buffer + SSE subscriber queues."""

    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, maxlen: int | None = None):
        super().__init__()
        self._maxlen = maxlen
        self._buffer = [] if maxlen is None else collections.deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop = None
        self._seq = 0

    def set_loop(self, loop):
        self._loop = loop

    def emit(self, record):
        message = self._ANSI_RE.sub("", record.getMessage())
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "message": message,
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buffer.append(entry)
            if self._loop is None:
                return
            stale = set()
            for q in self._subscribers:
                try:
                    self._loop.call_soon_threadsafe(q.put_nowait, entry)
                except Exception:
                    stale.add(q)
            self._subscribers -= stale

    def subscribe(self):
        q = asyncio.Queue()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    def history(self, since: int = 0):
        with self._lock:
            if since > 0 and self._buffer:
                first_seq = self._buffer[0]["seq"]
                start = max(0, since - first_seq + 1)
                if isinstance(self._buffer, list):
                    return self._buffer[start:].copy()
                return list(self._buffer)[start:]
            return list(self._buffer)


log_capture = LogCaptureHandler()
logging.getLogger().addHandler(log_capture)


def _bool(name, default):
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    upstream_url: str = os.getenv("UPSTREAM_URL", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2").rstrip("/")
    listen_host: str = os.getenv("LISTEN_HOST", "0.0.0.0")
    listen_port: int = int(os.getenv("LISTEN_PORT", "8080"))
    uvicorn_loop: str = os.getenv("UVICORN_LOOP", "auto").strip().lower() or "auto"
    retry_interval: float = float(os.getenv("RETRY_INTERVAL", "1.0"))
    retry_interval_429: float = float(os.getenv("RETRY_INTERVAL_429", "5.0"))
    retry_backoff_429: bool = _bool("RETRY_BACKOFF_429", "true")
    retry_backoff_max_429: float = float(os.getenv("RETRY_BACKOFF_MAX_429", "60"))
    retry_backoff: bool = _bool("RETRY_BACKOFF", "false")
    retry_backoff_max: float = float(os.getenv("RETRY_BACKOFF_MAX", "60"))
    # Retry-After 头封顶值（秒）；0 表示不封顶，完全尊重上游
    retry_after_max: float = float(os.getenv("RETRY_AFTER_MAX", "0"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "60"))
    retry_status_codes: frozenset = frozenset(int(x) for x in os.getenv("RETRY_STATUS_CODES", "503,502,504,524,529,429").split(",") if x.strip())
    retry_broad: bool = _bool("RETRY_BROAD", "false")
    timeout: float = float(os.getenv("TIMEOUT", "300"))
    connect_timeout: float = float(os.getenv("CONNECT_TIMEOUT", "10"))
    max_request_body: int = int(os.getenv("MAX_REQUEST_BODY", str(64 * 1024 * 1024)))
    responses_header_timeout: float = float(os.getenv("RESPONSES_HEADER_TIMEOUT", "120"))
    responses_attempt_header_timeout: float = float(os.getenv("RESPONSES_ATTEMPT_HEADER_TIMEOUT", "15"))
    responses_attempt_header_timeout_body_limit: int = int(os.getenv(
        "RESPONSES_ATTEMPT_HEADER_TIMEOUT_BODY_LIMIT", str(1024 * 1024),
    ))
    provider: str = os.getenv("PROVIDER", "xfyun")
    extra_upstreams: str = os.getenv("EXTRA_UPSTREAMS", "")
    log_dir: str = os.getenv("LOG_DIR", "logs")
    log_retention_days: int = int(os.getenv("LOG_RETENTION_DAYS", "30"))
    legacy_log_file: str = os.getenv("LOG_FILE", "retry_log.jsonl")
    hedge_mode: str = os.getenv("HEDGE_MODE", "off").lower()
    max_concurrent: int = int(os.getenv("MAX_CONCURRENT", "10"))
    trust_env: bool = _bool("TRUST_ENV", "false")
    admin_password: str = (os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_TOKEN", "")).strip()
    admin_cookie_secure: bool = _bool("ADMIN_COOKIE_SECURE", "false")
    settings_page_enabled: bool = _bool("SETTINGS_PAGE_ENABLED", "false")
    proxy_api_key: str = os.getenv("PROXY_API_KEY", "").strip()
    ip_blacklist: tuple = parse_ip_networks(os.getenv("IP_BLACKLIST", ""), "IP_BLACKLIST")
    trusted_proxy_ips: tuple = parse_ip_networks(
        os.getenv("TRUSTED_PROXY_IPS", "127.0.0.0/8,::1,172.16.0.0/12"),
        "TRUSTED_PROXY_IPS",
    )
    ip_auto_ban_threshold: int = int(os.getenv("IP_AUTO_BAN_THRESHOLD", "20"))
    ip_auto_ban_window: float = float(os.getenv("IP_AUTO_BAN_WINDOW", "10"))
    ip_auto_ban_duration: float = float(os.getenv("IP_AUTO_BAN_DURATION", "0"))
    ip_auto_ban_exempt: tuple = parse_ip_networks(
        os.getenv("IP_AUTO_BAN_EXEMPT", "127.0.0.0/8,::1"), "IP_AUTO_BAN_EXEMPT",
    )
    ip_ban_state_file: str = os.getenv("IP_BAN_STATE_FILE", "").strip() or os.path.join(
        os.getenv("LOG_DIR", "logs"), ".ip_bans.json"
    )
    key_pools_raw: str = os.getenv("KEY_POOLS", "").strip()
    key_pool_file: str = os.getenv("KEY_POOL_FILE", "").strip()
    key_cooldown: float = float(os.getenv("KEY_COOLDOWN", "30"))
    key_cooldown_5xx: float = float(os.getenv("KEY_COOLDOWN_5XX", os.getenv("KEY_COOLDOWN", "30")))
    key_cooldown_429: float = float(os.getenv("KEY_COOLDOWN_429", "60"))
    key_cooldown_auth: float = float(os.getenv("KEY_COOLDOWN_AUTH", "1800"))
    key_cooldown_max: float = float(os.getenv("KEY_COOLDOWN_MAX", "3600"))
    key_cooldown_backoff: bool = _bool("KEY_COOLDOWN_BACKOFF", "true")
    key_sticky: float = float(os.getenv("KEY_STICKY", "120"))
    key_pool_wait_timeout: float = float(os.getenv("KEY_POOL_WAIT_TIMEOUT", "120"))
    key_ttft_stale_after: float = float(os.getenv("KEY_TTFT_STALE_AFTER", "300"))
    key_ttft_retest_interval: float = float(os.getenv("KEY_TTFT_RETEST_INTERVAL", "60"))
    key_ttft_confirmations: int = int(os.getenv("KEY_TTFT_CONFIRMATIONS", "2"))
    key_ttft_hysteresis: float = float(os.getenv("KEY_TTFT_HYSTERESIS", "0.1"))
    key_cache_miss_threshold: int = int(os.getenv("KEY_CACHE_MISS_THRESHOLD", "3"))
    key_cache_miss_min_input_tokens: int = int(os.getenv("KEY_CACHE_MISS_MIN_INPUT_TOKENS", "1024"))
    key_cache_miss_cooldown: float = float(os.getenv("KEY_CACHE_MISS_COOLDOWN", "3600"))
    key_auth_header: str = os.getenv("KEY_AUTH_HEADER", "authorization").lower()
    key_auth_scheme: str = os.getenv("KEY_AUTH_SCHEME", "Bearer")
    key_pool_sync_default_adapter: str = os.getenv("KEY_POOL_SYNC_DEFAULT_ADAPTER", "sub2api").strip().lower()
    key_pool_sync_default_url: str = os.getenv(
        "KEY_POOL_SYNC_URL", os.getenv("UPSTREAM_URL", "")
    ).strip().rstrip("/")
    key_pool_sync_interval: int = int(os.getenv("KEY_POOL_SYNC_INTERVAL", "300"))
    key_pool_create_delay: float = float(os.getenv("KEY_POOL_CREATE_DELAY", "1.5"))
    image_upstream_user_agent: str = os.getenv("IMAGE_UPSTREAM_USER_AGENT", "").strip()
    image_upstream_originator: str = os.getenv("IMAGE_UPSTREAM_ORIGINATOR", "").strip()
    key_pool_sync_state_file: str = os.getenv("KEY_POOL_SYNC_STATE_FILE", "").strip() or os.path.join(
        os.getenv("LOG_DIR", "logs"), ".key_pool_sync.json"
    )
    key_pool_sync_secret: str = (os.getenv("KEY_POOL_SYNC_SECRET") or os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_TOKEN", "")).strip()
    dlp_mode: str = os.getenv("DLP_MODE", "off").lower()
    dlp_rules: frozenset = frozenset(x.strip() for x in os.getenv("DLP_RULES", "private_key,ai_tokens,code_tokens,cloud_tokens,saas_tokens,package_tokens,credentials,csv_credentials,jwt,connection_string,id_card,bank_card,structured_secret").split(",") if x.strip())
    dlp_rule_file: str = os.getenv("DLP_RULE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dlp_rules.yaml"))
    dlp_exempt_start: str = os.getenv("DLP_EXEMPT_START", "[[ALLOW_SENSITIVE]]")
    dlp_exempt_end: str = os.getenv("DLP_EXEMPT_END", "[[/ALLOW_SENSITIVE]]")
    dlp_allow_exemptions: bool = _bool("DLP_ALLOW_EXEMPTIONS", "false")
    dlp_strip_exempt_markers: bool = _bool("DLP_STRIP_EXEMPT_MARKERS", "true")
    dlp_max_body_bytes: int = int(os.getenv("DLP_MAX_BODY_BYTES", "16777216"))
    dlp_decode_depth: int = int(os.getenv("DLP_DECODE_DEPTH", "2"))
    dlp_decode_max_candidates: int = int(os.getenv("DLP_DECODE_MAX_CANDIDATES", "100"))
    dlp_decode_max_bytes: int = int(os.getenv("DLP_DECODE_MAX_BYTES", "1048576"))
    dlp_known_secret_min_length: int = int(os.getenv("DLP_KNOWN_SECRET_MIN_LENGTH", "8"))
    dlp_fail_closed: bool = _bool("DLP_FAIL_CLOSED", "false")

    # Token 统计：对 OpenAI Chat 流式请求且未带 stream_options 的，自动注入
    # stream_options={"include_usage":true} 以确保末帧返回 usage。关闭则保持
    # 请求体原样透传，stream 场景的 token 统计可能缺失。默认关闭以保持透明代理。
    token_stats_inject_usage: bool = _bool("TOKEN_STATS_INJECT_USAGE", "false")

    # Codex Responses WebSocket -> upstream HTTP/SSE bridge
    sse2ws_mode: str = os.getenv("SSE2WS_MODE", "off").strip().lower()
    sse2ws_first_event_timeout: float = float(os.getenv("SSE2WS_FIRST_EVENT_TIMEOUT", "30"))
    sse2ws_first_event_retries: int = int(os.getenv("SSE2WS_FIRST_EVENT_RETRIES", "2"))
    sse2ws_first_message_timeout: float = float(os.getenv("SSE2WS_FIRST_MESSAGE_TIMEOUT", "30"))
    sse2ws_inter_turn_idle_timeout: float = float(os.getenv("SSE2WS_INTER_TURN_IDLE_TIMEOUT", "300"))
    sse2ws_max_body_bytes: int = int(os.getenv("SSE2WS_MAX_BODY_BYTES", str(64 * 1024 * 1024)))

    @property
    def stats_html_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stats.html")

    @property
    def logs_html_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs.html")

    @property
    def key_pool_html_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "key_pool.html")

    @property
    def settings_html_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.html")

    @property
    def summary_file(self):
        return os.path.join(self.log_dir, "_summary.json")

    def apply_env(self, overrides: dict) -> list:
        """按环境变量键热更新配置项

        仅处理 HOT_PARSERS 中登记的键与 PROVIDER_ALIASES；其余键直接忽略。
        更新字段的同时同步 os.environ，保证直接读取环境变量的模块（如
        stats.py 的 PROVIDER_ALIASES）与 Settings 保持一致。
        返回实际生效的键列表。
        """
        applied = []
        for key, raw in overrides.items():
            if key == "PROVIDER_ALIASES":
                os.environ[key] = str(raw).strip()
                from .stats import reload_aliases

                reload_aliases()
                applied.append(key)
                continue
            parser = HOT_PARSERS.get(key)
            if parser is None:
                continue
            attr, parse = parser
            os.environ[key] = str(raw)
            setattr(self, attr, parse(raw))
            applied.append(key)
        return applied


def _parse_bool(raw) -> bool:
    return str(raw).lower() in ("1", "true", "yes", "on")


def _parse_csv_int(raw) -> frozenset:
    return frozenset(int(x) for x in str(raw).split(",") if x.strip())


def _parse_csv_str(raw) -> frozenset:
    return frozenset(x.strip() for x in str(raw).split(",") if x.strip())


# 支持热更新的配置键 -> (Settings 属性名, 解析函数)；与 settings_meta 的
# hot 项一一对应，新增热更新键时必须同时登记两处
HOT_PARSERS = {
    "MAX_REQUEST_BODY": ("max_request_body", int),
    "MAX_RETRIES": ("max_retries", int),
    "RETRY_STATUS_CODES": ("retry_status_codes", _parse_csv_int),
    "RETRY_BROAD": ("retry_broad", _parse_bool),
    "RETRY_INTERVAL": ("retry_interval", float),
    "RETRY_BACKOFF": ("retry_backoff", _parse_bool),
    "RETRY_BACKOFF_MAX": ("retry_backoff_max", float),
    "RETRY_INTERVAL_429": ("retry_interval_429", float),
    "RETRY_BACKOFF_429": ("retry_backoff_429", _parse_bool),
    "RETRY_BACKOFF_MAX_429": ("retry_backoff_max_429", float),
    "RETRY_AFTER_MAX": ("retry_after_max", float),
    "HEDGE_MODE": ("hedge_mode", lambda v: str(v).strip().lower()),
    "MAX_CONCURRENT": ("max_concurrent", int),
    "KEY_COOLDOWN": ("key_cooldown", float),
    "KEY_COOLDOWN_5XX": ("key_cooldown_5xx", float),
    "KEY_COOLDOWN_429": ("key_cooldown_429", float),
    "KEY_COOLDOWN_AUTH": ("key_cooldown_auth", float),
    "KEY_COOLDOWN_MAX": ("key_cooldown_max", float),
    "KEY_COOLDOWN_BACKOFF": ("key_cooldown_backoff", _parse_bool),
    "KEY_STICKY": ("key_sticky", float),
    "KEY_POOL_WAIT_TIMEOUT": ("key_pool_wait_timeout", float),
    "KEY_TTFT_STALE_AFTER": ("key_ttft_stale_after", float),
    "KEY_TTFT_RETEST_INTERVAL": ("key_ttft_retest_interval", float),
    "KEY_TTFT_CONFIRMATIONS": ("key_ttft_confirmations", int),
    "KEY_TTFT_HYSTERESIS": ("key_ttft_hysteresis", float),
    "KEY_CACHE_MISS_THRESHOLD": ("key_cache_miss_threshold", int),
    "KEY_CACHE_MISS_MIN_INPUT_TOKENS": ("key_cache_miss_min_input_tokens", int),
    "KEY_CACHE_MISS_COOLDOWN": ("key_cache_miss_cooldown", float),
    "TOKEN_STATS_INJECT_USAGE": ("token_stats_inject_usage", _parse_bool),
    "IMAGE_UPSTREAM_USER_AGENT": ("image_upstream_user_agent", lambda v: str(v).strip()),
    "IMAGE_UPSTREAM_ORIGINATOR": ("image_upstream_originator", lambda v: str(v).strip()),
    "DLP_MODE": ("dlp_mode", lambda v: str(v).strip().lower()),
    "DLP_RULES": ("dlp_rules", _parse_csv_str),
    "DLP_ALLOW_EXEMPTIONS": ("dlp_allow_exemptions", _parse_bool),
    "DLP_EXEMPT_START": ("dlp_exempt_start", str),
    "DLP_EXEMPT_END": ("dlp_exempt_end", str),
    "DLP_STRIP_EXEMPT_MARKERS": ("dlp_strip_exempt_markers", _parse_bool),
    "DLP_MAX_BODY_BYTES": ("dlp_max_body_bytes", int),
    "DLP_DECODE_DEPTH": ("dlp_decode_depth", int),
    "DLP_DECODE_MAX_CANDIDATES": ("dlp_decode_max_candidates", int),
    "DLP_DECODE_MAX_BYTES": ("dlp_decode_max_bytes", int),
    "DLP_KNOWN_SECRET_MIN_LENGTH": ("dlp_known_secret_min_length", int),
    "DLP_FAIL_CLOSED": ("dlp_fail_closed", _parse_bool),
    "PROXY_API_KEY": ("proxy_api_key", lambda v: str(v).strip()),
}


settings = Settings()


def admin_session_value(password=None):
    secret = settings.admin_password if password is None else password
    return hashlib.sha256(f"llm-retry-proxy-session:{secret}".encode("utf-8")).hexdigest()


def require_admin(request: Request):
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="admin_auth_not_configured")
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    bearer_ok = (scheme.lower() == "bearer" and credential
                 and secrets.compare_digest(credential, settings.admin_password))
    session = request.cookies.get("admin_session", "")
    cookie_ok = bool(session and secrets.compare_digest(session, admin_session_value()))
    if bearer_ok or cookie_ok:
        return
    if request.url.path in ("/stats", "/logs", "/key-pools", "/settings"):
        raise HTTPException(status_code=303, headers={"Location": f"/admin/login?next={request.url.path}"})
    raise HTTPException(status_code=401, detail="invalid_admin_credentials",
                        headers={"WWW-Authenticate": "Bearer"})


def can_use_key_pool(headers) -> bool:
    if not settings.proxy_api_key:
        return True
    scheme, _, credential = headers.get("authorization", "").partition(" ")
    return bool(scheme.lower() == "bearer" and credential
                and secrets.compare_digest(credential, settings.proxy_api_key))


def should_retry_status(status: int) -> bool:
    return status >= 500 or status in (429, 401, 403) if settings.retry_broad else status in settings.retry_status_codes
