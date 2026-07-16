import asyncio
import collections
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from io import StringIO

from dotenv import load_dotenv


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
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), handlers=[_h])
logger = logging.getLogger("forward")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


class LogCaptureHandler(logging.Handler):
    """Thread-safe log handler: ring buffer + SSE subscriber queues."""

    _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, maxlen: int = 2000):
        super().__init__()
        self._maxlen = maxlen
        self._buffer: collections.deque = collections.deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop = None
        self._seq = 0

    def set_loop(self, loop):
        self._loop = loop

    def emit(self, record):
        with self._lock:
            self._seq += 1
            seq = self._seq
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "message": self._ANSI_RE.sub("", record.getMessage()),
            "seq": seq,
        }
        with self._lock:
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

    def history(self):
        with self._lock:
            return list(self._buffer)


log_capture = LogCaptureHandler(maxlen=2000)
logging.getLogger().addHandler(log_capture)


def _bool(name, default):
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    upstream_url: str = os.getenv("UPSTREAM_URL", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2").rstrip("/")
    listen_host: str = os.getenv("LISTEN_HOST", "0.0.0.0")
    listen_port: int = int(os.getenv("LISTEN_PORT", "8080"))
    retry_interval: float = float(os.getenv("RETRY_INTERVAL", "1.0"))
    retry_interval_429: float = float(os.getenv("RETRY_INTERVAL_429", "5.0"))
    retry_backoff_429: bool = _bool("RETRY_BACKOFF_429", "true")
    retry_backoff_max_429: float = float(os.getenv("RETRY_BACKOFF_MAX_429", "60"))
    retry_backoff: bool = _bool("RETRY_BACKOFF", "false")
    retry_backoff_max: float = float(os.getenv("RETRY_BACKOFF_MAX", "60"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "60"))
    retry_status_codes: frozenset = frozenset(int(x) for x in os.getenv("RETRY_STATUS_CODES", "503,502,504,524,529,429").split(",") if x.strip())
    retry_broad: bool = _bool("RETRY_BROAD", "false")
    timeout: float = float(os.getenv("TIMEOUT", "300"))
    connect_timeout: float = float(os.getenv("CONNECT_TIMEOUT", "10"))
    provider: str = os.getenv("PROVIDER", "xfyun")
    extra_upstreams: str = os.getenv("EXTRA_UPSTREAMS", "")
    log_dir: str = os.getenv("LOG_DIR", "logs")
    log_retention_days: int = int(os.getenv("LOG_RETENTION_DAYS", "30"))
    legacy_log_file: str = os.getenv("LOG_FILE", "retry_log.jsonl")
    hedge_mode: str = os.getenv("HEDGE_MODE", "off").lower()
    max_concurrent: int = int(os.getenv("MAX_CONCURRENT", "10"))
    trust_env: bool = _bool("TRUST_ENV", "false")
    key_pools_raw: str = os.getenv("KEY_POOLS", "").strip()
    key_pool_file: str = os.getenv("KEY_POOL_FILE", "").strip()
    key_cooldown: float = float(os.getenv("KEY_COOLDOWN", "30"))
    key_sticky: float = float(os.getenv("KEY_STICKY", "60"))
    key_auth_header: str = os.getenv("KEY_AUTH_HEADER", "authorization").lower()
    key_auth_scheme: str = os.getenv("KEY_AUTH_SCHEME", "Bearer")

    @property
    def stats_html_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stats.html")

    @property
    def logs_html_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs.html")

    @property
    def summary_file(self):
        return os.path.join(self.log_dir, "_summary.json")


settings = Settings()


def should_retry_status(status: int) -> bool:
    return status >= 500 or status in (429, 401, 403) if settings.retry_broad else status in settings.retry_status_codes
