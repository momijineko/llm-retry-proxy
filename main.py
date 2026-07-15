import os
import sys
import csv
import asyncio
import logging
import time
import json
import random
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

try:
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    getattr(sys.stderr, "reconfigure", lambda **_: None)(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
from io import StringIO


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

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response

if sys.platform == "win32":
    os.system("")


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

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2").rstrip("/")
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))
RETRY_INTERVAL = float(os.getenv("RETRY_INTERVAL", "1.0"))
RETRY_INTERVAL_429 = float(os.getenv("RETRY_INTERVAL_429", "5.0"))
RETRY_BACKOFF_429 = os.getenv("RETRY_BACKOFF_429", "true").lower() in ("1", "true", "yes", "on")
RETRY_BACKOFF_MAX_429 = float(os.getenv("RETRY_BACKOFF_MAX_429", "60"))
RETRY_BACKOFF = os.getenv("RETRY_BACKOFF", "false").lower() in ("1", "true", "yes", "on")
RETRY_BACKOFF_MAX = float(os.getenv("RETRY_BACKOFF_MAX", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "60"))
RETRY_STATUS_CODES = set(
    int(x) for x in os.getenv("RETRY_STATUS_CODES", "503,502,504,524,529,429").split(",") if x.strip()
)

# 宽松重试/换key模式: on=按规则触发(5xx+429+401/403+网络异常), off=仅 RETRY_STATUS_CODES 白名单
RETRY_BROAD = os.getenv("RETRY_BROAD", "false").lower() in ("1", "true", "yes", "on")


def _should_retry_status(status: int) -> bool:
    """是否触发重试+换key。
    宽松模式: 5xx / 429 / 401 / 403 全算（服务端过载+限流+鉴权失败），排除 400/404/422 等请求级错误。
    非宽松模式: 仅 RETRY_STATUS_CODES 白名单（向后兼容）。
    """
    if RETRY_BROAD:
        return status >= 500 or status in (429, 401, 403)
    return status in RETRY_STATUS_CODES


def _is_host_level_error(exc: Exception) -> bool:
    """连接建立阶段的错误(DNS解析失败/连接被拒/连接超时),与 key 无关,换 key 无意义。"""
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


TIMEOUT = float(os.getenv("TIMEOUT", "300"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "10"))
PROVIDER = os.getenv("PROVIDER", "xfyun")
EXTRA_UPSTREAMS = os.getenv("EXTRA_UPSTREAMS", "")
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LEGACY_LOG_FILE = os.getenv("LOG_FILE", "retry_log.jsonl")
HEDGE_MODE = os.getenv("HEDGE_MODE", "off").lower()  # off / race / stagger
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "10"))
TRUST_ENV = os.getenv("TRUST_ENV", "false").lower() in ("1", "true", "yes", "on")
STATS_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.html")
SUMMARY_FILE = os.path.join(LOG_DIR, "_summary.json")

# ── 号池（Key Pool）配置 ──
# 号池原始配置：按倍率从低到高排列 key（cheap→expensive），留空则保持原有透传客户端 key 行为
KEY_POOLS_RAW = os.getenv("KEY_POOLS", "").strip()
# 号池 CSV 文件路径（优先于 KEY_POOLS 环境变量）。格式: key,url,provider（首行表头，行序=优先级）
KEY_POOL_FILE = os.getenv("KEY_POOL_FILE", "").strip()
# 单个 key 遇到 429/5xx 后的冷却时间（秒），冷却期间优先跳过该 key
KEY_COOLDOWN = float(os.getenv("KEY_COOLDOWN", "30"))
# key 粘性持续时间（秒）。选定一个 key 后保持使用直到该时间过期或 key 被限流，避免频繁切换导致上游缓存失效
KEY_STICKY = float(os.getenv("KEY_STICKY", "60"))
# 注入鉴权头的 header 名（默认 authorization）
KEY_AUTH_HEADER = os.getenv("KEY_AUTH_HEADER", "authorization").lower()
# 鉴权 scheme 前缀（默认 "Bearer"，设为空则只放裸 key）
KEY_AUTH_SCHEME = os.getenv("KEY_AUTH_SCHEME", "Bearer")

SKIP_REQUEST_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "upgrade",
}
SKIP_RESPONSE_HEADERS = {
    "content-length", "transfer-encoding", "connection",
    "keep-alive", "content-encoding",
}

EXCLUDE_PATHS = {
    "favicon.ico", "robots.txt", "sitemap.xml",
    "manifest.json", "site.webmanifest", "browserconfig.xml",
}


def _is_excluded_path(path: str) -> bool:
    return path.lstrip("/").lower() in EXCLUDE_PATHS


def _build_routes() -> list:
    routes = []
    raw = EXTRA_UPSTREAMS.strip()
    if raw:
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split("|")
            if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
                logger.warning(f"EXTRA_UPSTREAMS 条目格式错误，跳过: {entry!r}（应为 prefix|url|provider）")
                continue
            prefix = parts[0].strip()
            url = parts[1].strip().rstrip("/")
            provider = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else prefix.lstrip("/")
            routes.append((prefix, url, provider, True))
    routes.sort(key=lambda r: len(r[0]), reverse=True)
    routes.append(("", UPSTREAM_URL, PROVIDER, False))
    return routes


ROUTES = _build_routes()


def match_route(path: str):
    for prefix, upstream_url, provider, strip in ROUTES:
        if not prefix:
            return upstream_url, provider, path
        pfx = prefix.lstrip("/")
        if not pfx:
            continue
        if path == pfx or path.startswith(pfx + "/"):
            remaining = path[len(pfx):].lstrip("/") if strip else path
            return upstream_url, provider, remaining
    return UPSTREAM_URL, PROVIDER, path


# ── 号池（Key Pool）──
# 每个 KeyEntry 对应一个上游 key，cooldown_until 为软冷却截止时间戳。
# 软冷却：pick() 优先返回非冷却 key，全部冷却时返回最快到期的（不阻塞请求）。

class KeyEntry:
    __slots__ = ("key", "key_id", "label", "cooldown_until", "total_fail", "last_fail_ts")

    def __init__(self, key: str, label: str = ""):
        self.key = key
        self.label = label
        self.key_id = label if label else key[:8]
        self.cooldown_until = 0.0
        self.total_fail = 0
        self.last_fail_ts = 0.0


class KeyPool:
    """按优先级（cheap→expensive）排列的 key 池，支持软冷却降级和粘性保持。"""

    def __init__(self, keys, provider: str = ""):
        """keys: list[str] 或 list[(key, label)] 元组。"""
        self.entries: list[KeyEntry] = []
        for k in keys:
            if isinstance(k, tuple):
                self.entries.append(KeyEntry(k[0], k[1] if len(k) > 1 else ""))
            else:
                self.entries.append(KeyEntry(k))
        self.provider = provider
        self._current: Optional[KeyEntry] = None
        self._sticky_until: float = 0.0

    def pick(self) -> Optional[KeyEntry]:
        """返回最佳 key。

        粘性逻辑（KEY_STICKY > 0 时）：选定一个 key 后保持使用，直到粘性过期或该 key 被冷却。
        粘性期间即使更便宜的 key 恢复了也不切回，避免频繁切换导致上游缓存失效。
        粘性过期或当前 key 被冷却时，重新选最便宜可用的 key。
        """
        now = time.time()
        # 粘性有效且当前 key 未冷却 → 保持
        if (
            self._current is not None
            and now < self._sticky_until
            and self._current.cooldown_until <= now
        ):
            return self._current
        # 重新选最便宜可用的
        for e in self.entries:
            if e.cooldown_until <= now:
                self._current = e
                self._sticky_until = now + KEY_STICKY
                return e
        # 全部冷却 → 返回最快到期的（软冷却）
        best = min(self.entries, key=lambda e: e.cooldown_until) if self.entries else None
        if best is not None:
            self._current = best
            self._sticky_until = now + KEY_STICKY
        return best

    def has_fresh(self) -> bool:
        """是否有未冷却的 key。"""
        now = time.time()
        return any(e.cooldown_until <= now for e in self.entries)

    def mark_cooldown(self, entry: KeyEntry, seconds: float, ra_wait: Optional[float] = None):
        """标记 key 冷却，冷却时间取 max(seconds, Retry-After)。"""
        wait = max(seconds, ra_wait or 0.0)
        now = time.time()
        already_cooling = entry.cooldown_until > now
        entry.cooldown_until = now + wait
        if not already_cooling:
            entry.total_fail += 1
        entry.last_fail_ts = now

    def status(self) -> list:
        now = time.time()
        return [
            {
                "key_id": e.key_id,
                "label": e.label,
                "cooled": e.cooldown_until > now,
                "cooldown_remaining": round(max(e.cooldown_until - now, 0), 1),
                "total_fail": e.total_fail,
            }
            for e in self.entries
        ]


def _resolve_path(path: str) -> Optional[str]:
    """解析文件路径：相对路径先试 CWD 再试脚本目录，返回存在的绝对路径或 None。"""
    if os.path.isabs(path):
        return path if os.path.exists(path) else None
    if os.path.exists(path):
        return os.path.abspath(path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(script_dir, path)
    return candidate if os.path.exists(candidate) else None


def _load_key_pools_csv(path: str) -> dict:
    """从 CSV 文件加载号池。

    格式: 首行为表头 key,url,provider（不区分大小写），# 开头行为注释。
    key 列必填，url/provider 为空时用默认值。行序即优先级（上=便宜，下=贵）。
    """
    pools: dict[str, KeyPool] = {}
    fpath = _resolve_path(path)
    if fpath is None:
        logger.warning(f"KEY_POOL_FILE 文件不存在: {path}")
        return pools

    raw = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(fpath, "r", encoding=enc) as f:
                raw = f.read()
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        logger.warning(f"KEY_POOL_FILE 编码无法识别(非 UTF-8/GBK): {fpath}")
        return pools

    # 过滤注释行和空白行（含只有逗号的行）
    lines = [
        line for line in raw.splitlines()
        if line.strip().strip(",") and not line.strip().startswith("#")
    ]
    if not lines:
        logger.warning(f"KEY_POOL_FILE 内容为空: {fpath}")
        return pools

    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        logger.warning(f"KEY_POOL_FILE 无表头，跳过: {fpath}")
        return pools
    # 列名归一化（小写去空格）
    reader.fieldnames = [h.strip().lower() if h else h for h in reader.fieldnames]
    if "key" not in reader.fieldnames:
        logger.warning(f"KEY_POOL_FILE 缺少 key 列，跳过: {fpath}")
        return pools

    for row in reader:
        key = (row.get("key") or "").strip()
        if not key:
            continue
        url = (row.get("url") or "").strip().rstrip("/") or UPSTREAM_URL
        provider = (row.get("provider") or "").strip() or PROVIDER
        label = (row.get("label") or "").strip()
        entry = (key, label) if label else key
        if url in pools:
            pools[url].entries.append(KeyEntry(key, label))
            if provider and pools[url].provider != provider:
                logger.warning(f"号池 key={label or key[:8]} 的 provider={provider!r} 与池现有={pools[url].provider!r} 不一致，已忽略")
        else:
            pools[url] = KeyPool([entry], provider)

    if pools:
        total = sum(len(p.entries) for p in pools.values())
        logger.info(f"号池CSV已加载: {fpath} ({len(pools)}个上游, 共{total}个key)")
    return pools


def _build_key_pools() -> dict:
    """解析号池配置，返回 {upstream_url: KeyPool}。

    优先级: KEY_POOL_FILE (CSV) > KEY_POOLS (环境变量)。
    两者都配置时 CSV 优先，环境变量被忽略（打印警告）。
    """
    # CSV 优先
    if KEY_POOL_FILE:
        pools = _load_key_pools_csv(KEY_POOL_FILE)
        if pools:
            if KEY_POOLS_RAW:
                logger.warning("同时配置了 KEY_POOL_FILE 和 KEY_POOLS，已使用 CSV 文件，KEY_POOLS 被忽略")
            return pools
        # CSV 加载失败（文件不存在/格式错误），继续尝试环境变量

    # 环境变量回退
    pools: dict[str, KeyPool] = {}
    raw = KEY_POOLS_RAW
    if not raw:
        return pools
    for group in raw.split(","):
        group = group.strip()
        if not group:
            continue
        if "|" in group:
            parts = group.split("|")
            if len(parts) < 3 or not parts[0].strip() or not parts[2].strip():
                logger.warning(f"KEY_POOLS 条目格式错误，跳过: {group!r}（应为 url|provider|key1;key2;...）")
                continue
            url = parts[0].strip().rstrip("/")
            provider = parts[1].strip()
            keys = [k.strip() for k in parts[2].split(";") if k.strip()]
            if not keys:
                continue
            pools[url] = KeyPool(keys, provider)
        else:
            keys = [k.strip() for k in group.split(";") if k.strip()]
            if not keys:
                continue
            pools[UPSTREAM_URL] = KeyPool(keys, PROVIDER)
    if pools:
        total = sum(len(p.entries) for p in pools.values())
        logger.info(f"号池已加载: {len(pools)}个上游, 共{total}个key")
    return pools


KEY_POOLS = _build_key_pools()

# 注入鉴权头时需要剥离的客户端原有 header（小写集合）
_AUTH_STRIP_HEADERS = {"authorization"}
if KEY_AUTH_HEADER != "authorization":
    _AUTH_STRIP_HEADERS.add(KEY_AUTH_HEADER)


def _headers_with_key(base_headers: dict, key: Optional[str]) -> dict:
    """剥离客户端鉴权头，注入号池 key。key 为 None 时仅剥离不注入。"""
    h = {k: v for k, v in base_headers.items() if k.lower() not in _AUTH_STRIP_HEADERS}
    if key:
        if KEY_AUTH_SCHEME:
            h[KEY_AUTH_HEADER] = f"{KEY_AUTH_SCHEME} {key}"
        else:
            h[KEY_AUTH_HEADER] = key
    return h


client: Optional[httpx.AsyncClient] = None
_log_lock = asyncio.Lock()
_summary_cache: Optional[dict] = None


def _daily_file_path(date_str: str) -> str:
    return os.path.join(LOG_DIR, f"retry_{date_str}.jsonl")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _new_summary() -> dict:
    return {
        "version": 5,
        "total_requests": 0, "total_retries": 0,
        "total_succeeded": 0, "total_failed": 0, "total_first_ok": 0,
        "by_provider": {}, "by_model": {}, "by_key": {}, "by_status": {},
        "first_ts": None, "last_ts": None,
    }


def _load_summary() -> dict:
    if os.path.exists(SUMMARY_FILE):
        try:
            with open(SUMMARY_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            for k in ("total_requests", "total_retries", "total_succeeded", "total_failed", "total_first_ok"):
                s.setdefault(k, 0)
            s.setdefault("version", 1)
            for k in ("by_provider", "by_model", "by_key", "by_status"):
                s.setdefault(k, {})
            s.setdefault("first_ts", None)
            s.setdefault("last_ts", None)
            return s
        except Exception as e:
            logger.warning(f"读取累计汇总失败，重新初始化: {e}")
    return _new_summary()


def _save_summary(summary: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = SUMMARY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
        os.replace(tmp, SUMMARY_FILE)
    except Exception as e:
        logger.warning(f"写累计汇总失败: {e}")


def _update_summary_mem(summary: dict, r: dict):
    summary["total_requests"] += 1
    summary["total_retries"] += r.get("retries", 0)
    if _req_succeeded(r):
        summary["total_succeeded"] += 1
        if r.get("first_ok", r.get("retries", 0) == 0):
            summary["total_first_ok"] = summary.get("total_first_ok", 0) + 1
    else:
        summary["total_failed"] += 1
    ts = r.get("ts")
    if ts:
        if not summary["first_ts"]:
            summary["first_ts"] = ts
        summary["last_ts"] = ts
    for fkey, k in (("by_provider", _normalize_provider(r.get("provider", "") or "(unknown)")),
                    ("by_model", _model_key(r)),
                    ("by_key", r.get("key_id", ""))):
        if not k:
            continue
        b = summary[fkey].setdefault(k, {
            "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "failed": 0, "max_retries": 0,
        })
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            b["succeeded"] += 1
            if r.get("first_ok", r.get("retries", 0) == 0):
                b["first_ok"] = b.get("first_ok", 0) + 1
        else:
            b["failed"] += 1
        b["max_retries"] = max(b["max_retries"], r.get("retries", 0))
    sc = str(r.get("upstream_status", 0))
    summary["by_status"][sc] = summary["by_status"].get(sc, 0) + 1
    for code in r.get("retry_codes", []):
        sc2 = str(code)
        summary["by_status"][sc2] = summary["by_status"].get(sc2, 0) + 1


def _rebuild_summary_from_files() -> dict:
    summary = _new_summary()
    if not os.path.isdir(LOG_DIR):
        return summary
    for fname in sorted(os.listdir(LOG_DIR)):
        if not (fname.startswith("retry_") and fname.endswith(".jsonl")):
            continue
        path = os.path.join(LOG_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if _is_excluded_path(rec.get("path", "")):
                            continue
                        if not rec.get("model"):
                            continue
                        _update_summary_mem(summary, rec)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue
    return summary


def _migrate_legacy_log():
    if not os.path.exists(LEGACY_LOG_FILE) or os.path.isdir(LEGACY_LOG_FILE):
        return
    daily_groups = defaultdict(list)
    migrated = 0
    try:
        with open(LEGACY_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = r.get("ts", "")
                date_str = ts[:10] if len(ts) >= 10 else "unknown"
                daily_groups[date_str].append(line)
                migrated += 1
    except Exception as e:
        logger.warning(f"读取旧日志文件失败，跳过迁移: {e}")
        return
    if migrated == 0:
        try:
            os.rename(LEGACY_LOG_FILE, LEGACY_LOG_FILE + ".bak")
        except Exception:
            pass
        return
    for date_str, recs in daily_groups.items():
        dst = _daily_file_path(date_str) if date_str != "unknown" else os.path.join(LOG_DIR, "retry_unknown.jsonl")
        if os.path.exists(dst):
            continue
        try:
            with open(dst, "w", encoding="utf-8") as f:
                f.write("\n".join(recs) + "\n")
        except Exception as e:
            logger.warning(f"迁移写入 {dst} 失败: {e}")
    try:
        os.rename(LEGACY_LOG_FILE, LEGACY_LOG_FILE + ".bak")
        logger.info(f"已迁移旧日志 {migrated} 条到 {LOG_DIR}/，旧文件重命名为 {LEGACY_LOG_FILE}.bak")
    except Exception as e:
        logger.warning(f"迁移后重命名旧文件失败: {e}")


def _cleanup_old_logs():
    if LOG_RETENTION_DAYS <= 0 or not os.path.isdir(LOG_DIR):
        return
    cutoff_str = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for fname in os.listdir(LOG_DIR):
        if not (fname.startswith("retry_") and fname.endswith(".jsonl")):
            continue
        date_part = fname[6:16]
        if len(date_part) == 10 and date_part < cutoff_str:
            try:
                os.remove(os.path.join(LOG_DIR, fname))
                removed += 1
            except Exception as e:
                logger.warning(f"清理旧日志 {fname} 失败: {e}")
    if removed:
        logger.info(f"已清理 {removed} 个过期日志文件(>{LOG_RETENTION_DAYS}天)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, _summary_cache
    os.makedirs(LOG_DIR, exist_ok=True)
    _migrate_legacy_log()
    if os.path.exists(SUMMARY_FILE):
        _summary_cache = _load_summary()
        if _summary_cache.get("version", 1) < 4:
            logger.info("累计汇总格式过旧，从日志重建...")
            _summary_cache = _rebuild_summary_from_files()
            if _summary_cache["total_requests"] > 0:
                _save_summary(_summary_cache)
    else:
        _summary_cache = _rebuild_summary_from_files()
        if _summary_cache["total_requests"] > 0:
            _save_summary(_summary_cache)
    _cleanup_old_logs()
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT, connect=CONNECT_TIMEOUT),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        trust_env=TRUST_ENV,
    )
    logger.info("=" * 60)
    logger.info(f"转发服务启动: http://{LISTEN_HOST}:{LISTEN_PORT}")
    for prefix, upstream_url, provider, _ in ROUTES:
        pool_tag = " 号池" if upstream_url in KEY_POOLS else ""
        if prefix:
            logger.info(f"  路由: {prefix}/* -> {upstream_url}/  (provider={provider}, 去前缀{pool_tag})")
        else:
            logger.info(f"  路由: /* -> {upstream_url}  (provider={provider}, 默认{pool_tag})")
    retry_desc = f"无限" if MAX_RETRIES <= 0 else str(MAX_RETRIES)
    mode_desc = {"off": "串行重试", "race": "请求竞速(一次并发)", "stagger": "滚动竞速(交错补发)"}.get(HEDGE_MODE, HEDGE_MODE)
    backoff_429_desc = f"指数退避(上限{RETRY_BACKOFF_MAX_429:.0f}s)" if RETRY_BACKOFF_429 else "固定间隔"
    backoff_desc = f"指数退避(上限{RETRY_BACKOFF_MAX:.0f}s)" if RETRY_BACKOFF else "固定间隔"
    logger.info(f"重试: 间隔={RETRY_INTERVAL}s+{backoff_desc}, 429={RETRY_INTERVAL_429}s+{backoff_429_desc}(优先Retry-After), 最大次数={retry_desc}, 状态码={sorted(RETRY_STATUS_CODES)}, 宽松={'开(5xx/429/401/403)' if RETRY_BROAD else '关'}")
    logger.info(f"模式: {mode_desc}" + (f", 最大并发={MAX_CONCURRENT}" if HEDGE_MODE != "off" else ""))
    logger.info(f"记录: provider={PROVIDER}, 日志目录={LOG_DIR}, 保留{LOG_RETENTION_DAYS}天")
    logger.info(f"代理: trust_env={'是(跟随系统代理)' if TRUST_ENV else '否(直连)'}")
    if KEY_POOLS:
        for kp_url, kp in KEY_POOLS.items():
            route_tag = "默认" if kp_url == UPSTREAM_URL else kp_url
            labels = ", ".join(e.key_id for e in kp.entries)
            logger.info(f"号池: {route_tag} provider={kp.provider or PROVIDER} keys={len(kp.entries)}个 冷却={KEY_COOLDOWN:.0f}s 粘性={KEY_STICKY:.0f}s 鉴权={KEY_AUTH_HEADER}({'有' if KEY_AUTH_SCHEME else '无'}scheme)")
    else:
        logger.info("号池: 未配置(透传客户端key)")
    logger.info(f"统计面板: http://127.0.0.1:{LISTEN_PORT}/stats")
    logger.info("=" * 60)
    yield
    if client:
        await client.aclose()
    if _summary_cache:
        _save_summary(_summary_cache)


app = FastAPI(title="llm-retry-proxy", lifespan=lifespan)


def filter_headers(headers, skip: set) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in skip}


def _tag(method: str, path: str, provider: str, model: str) -> str:
    m = f"{provider}/{model}" if model else (provider or "?")
    return f"[{method} /{path}] [\033[36m{m}\033[0m]"


def _sc(s) -> str:
    if s == 0:
        return "\033[91mERR\033[0m"
    if s < 300:
        return f"\033[32m{s}\033[0m"
    if s < 400:
        return f"\033[34m{s}\033[0m"
    if s < 500:
        return f"\033[33m{s}\033[0m"
    return f"\033[31m{s}\033[0m"


def parse_model(body: bytes) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            m = data.get("model")
            if isinstance(m, str) and m:
                return m
    except Exception:
        pass
    return ""


def parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    if not header_value:
        return None
    val = header_value.strip()
    try:
        return max(float(val), 0.0)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt is None:
            return None
        now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
        return max((dt - now).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _calc_backoff_wait(
    consecutive: int,
    base: float,
    max_cap: float,
    enabled: bool,
    ra_wait: Optional[float] = None,
) -> tuple[float, str]:
    """计算重试等待时间（指数退避）。返回 (wait_seconds, wait_source)。

    指数退避开启时：base * 2^(n-1)，上限 max_cap，含 ±20% 抖动（jitter）。
    抖动用于避免多 Agent 同步重试导致 429 雪崩。
    ra_wait（Retry-After）存在时取 max(ra_wait, backoff)，确保不短于服务端要求。
    退避关闭时：ra_wait 存在用 ra_wait，否则用 base（保持原固定间隔行为）。
    """
    if enabled and consecutive > 0:
        raw = base * (2 ** (consecutive - 1))
        capped = min(raw, max_cap)
        jittered = min(capped * random.uniform(0.8, 1.2), max_cap)
        if ra_wait is not None:
            if jittered > ra_wait:
                return jittered, "RA+EB"
            return ra_wait, "RA"
        return jittered, "EB"
    # 退避关闭：保持原行为
    if ra_wait is not None:
        return ra_wait, "RA"
    return base, ""


async def write_log(record: dict):
    if not record.get("model"):
        return
    line = json.dumps(record, ensure_ascii=False)
    ts = record.get("ts", "")
    date_str = ts[:10] if len(ts) >= 10 else _today_str()
    daily_path = _daily_file_path(date_str)
    async with _log_lock:
        try:
            with open(daily_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning(f"写重试日志失败: {e}")
        if _summary_cache is not None:
            _update_summary_mem(_summary_cache, record)
            _save_summary(_summary_cache)


def load_log_records(days: int = 1) -> list:
    records = []
    if not os.path.isdir(LOG_DIR):
        return records
    if days <= 0:
        files = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("retry_") and f.endswith(".jsonl"))
    else:
        today = datetime.now()
        files = [f"retry_{(today - timedelta(days=i)).strftime('%Y-%m-%d')}.jsonl" for i in range(days)]
    for fname in files:
        path = os.path.join(LOG_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if _is_excluded_path(rec.get("path", "")):
                            continue
                        if not rec.get("model"):
                            continue
                        rec["provider"] = _normalize_provider(rec.get("provider", ""))
                        records.append(rec)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"读取日志文件 {fname} 失败: {e}")
    return records


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _status_category(s) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "exception"
    if s == 0:
        return "exception"
    if s < 300:
        return "2xx"
    if s < 400:
        return "3xx"
    if s < 500:
        return "4xx"
    return "5xx"


def _req_succeeded(r: dict) -> bool:
    """请求是否成功：final_status < 400（2xx/3xx 成功，4xx/5xx 失败）"""
    return r.get("final_status", 0) < 400


_PROVIDER_ALIASES = {
    "anthropic": "xfyun",
    "讯飞星辰 Coding Plan": "xfyun",
}


def _normalize_provider(p: str) -> str:
    return _PROVIDER_ALIASES.get(p, p)


def _model_key(r: dict) -> str:
    p = _normalize_provider(r.get("provider", "") or "(unknown)")
    m = r.get("model", "") or "(unknown)"
    return f"{p}/{m}"


def _agg_by(records: list, key: str, label: str, key_fn=None):
    buckets = defaultdict(lambda: {
        "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "max_retries": 0, "fail": 0,
        "durations": [], "fail_statuses": Counter(),
    })
    for r in records:
        k = (key_fn(r) if key_fn else (r.get(key, "") or "(unknown)")) or "(unknown)"
        b = buckets[k]
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            b["succeeded"] += 1
            if r.get("first_ok", r.get("retries", 0) == 0):
                b["first_ok"] += 1
        else:
            b["fail"] += 1
        # 统计所有上游错误码（含重试过程中的），不限最终失败
        rc = r.get("retry_codes")
        if rc:
            for code in rc:
                b["fail_statuses"][code] += 1
        elif not _req_succeeded(r):
            b["fail_statuses"][r.get("upstream_status", 0)] += 1
        b["max_retries"] = max(b["max_retries"], r.get("retries", 0))
        d = r.get("duration_s")
        if isinstance(d, (int, float)):
            b["durations"].append(d)
    out = []
    for k, b in buckets.items():
        ds = sorted(b["durations"])
        n = len(ds)
        dom = b["fail_statuses"].most_common(1)
        out.append({
            label: k,
            "requests": b["requests"],
            "retries": b["retries"],
            "avg_retries": round(b["retries"] / b["requests"], 2) if b["requests"] else 0,
            "success_rate": round(b["succeeded"] / b["requests"], 4) if b["requests"] else 0,
            "availability_pct": round(b["succeeded"] / b["requests"] * 100, 2) if b["requests"] else 0,
            "upstream_availability_pct": round(b["first_ok"] / b["requests"] * 100, 2) if b["requests"] else 0,
            "failed": b["fail"],
            "max_retries": b["max_retries"],
            "avg_duration": round(sum(ds) / n, 3) if n else 0,
            "p95_duration": round(_percentile(ds, 0.95), 3) if n else 0,
            "max_duration": round(ds[-1], 3) if n else 0,
            "dominant_fail_status": dom[0][0] if dom else None,
            "fail_status_count": dom[0][1] if dom else 0,
        })
    out.sort(key=lambda x: x["requests"], reverse=True)
    return out


def _window_label(mins: int) -> str:
    if mins < 60:
        return f"近{mins}分钟"
    h = mins // 60
    if mins % 60 == 0:
        return f"近{h}小时"
    return f"近{mins}分钟"


def _upstream_window_stats(records: list) -> list:
    """计算不同时间窗口的上游可用率（滑动窗口，从当前时间向前推）。
    上游可用率 = 首次尝试即成功的请求占比（first_ok字段(旧日志回退retries==0) && final_status<400）。
    同时计算环比：与上一等长窗口（如近5分钟 vs 前5分钟）的百分点差值。
    下游经重试后基本都 100%，无参考意义，不计算。"""
    windows = [5, 15, 30, 60, 360, 1440]  # 5分钟/15分钟/30分钟/1小时/6小时/24小时
    now = datetime.now()
    parsed = []
    for r in records:
        ts = r.get("ts", "")
        try:
            parsed.append((datetime.fromisoformat(ts), r))
        except (ValueError, TypeError):
            continue
    result = []
    for mins in windows:
        cutoff = now - timedelta(minutes=mins)
        prev_cutoff = now - timedelta(minutes=mins * 2)
        total = 0
        first_ok = 0
        prev_total = 0
        prev_first_ok = 0
        for t, r in parsed:
            if t >= cutoff:
                total += 1
                if _req_succeeded(r) and r.get("first_ok", r.get("retries", 0) == 0):
                    first_ok += 1
            elif t >= prev_cutoff:
                prev_total += 1
                if _req_succeeded(r) and r.get("first_ok", r.get("retries", 0) == 0):
                    prev_first_ok += 1
        cur_ua = round(first_ok / total * 100, 2) if total else None
        prev_ua = round(prev_first_ok / prev_total * 100, 2) if prev_total else None
        result.append({
            "window": mins,
            "label": _window_label(mins),
            "requests": total,
            "upstream_availability_pct": cur_ua,
            "prev_upstream_availability_pct": prev_ua,
            "upstream_diff": round(cur_ua - prev_ua, 2) if (cur_ua is not None and prev_ua is not None) else None,
        })
    return result


MODE_LABELS = {
    "off": "串行重试",
    "race": "请求竞速",
    "stagger": "滚动竞速",
}


def _mode_comparison(records: list) -> list:
    """按竞速模式聚合对比数据。旧日志无 mode 字段的归入 'off'（串行）。"""
    buckets = defaultdict(lambda: {
        "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0,
        "max_retries": 0, "fail": 0, "durations": [],
    })
    for r in records:
        m = r.get("mode", "") or "off"
        b = buckets[m]
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            b["succeeded"] += 1
            if r.get("first_ok", r.get("retries", 0) == 0):
                b["first_ok"] += 1
        else:
            b["fail"] += 1
        b["max_retries"] = max(b["max_retries"], r.get("retries", 0))
        d = r.get("duration_s")
        if isinstance(d, (int, float)):
            b["durations"].append(d)
    # 固定顺序：off / race / stagger
    order = ["off", "race", "stagger"]
    out = []
    for m in order:
        if m not in buckets:
            continue
        b = buckets[m]
        ds = sorted(b["durations"])
        n = len(ds)
        out.append({
            "mode": m,
            "mode_label": MODE_LABELS.get(m, "未知/旧数据"),
            "requests": b["requests"],
            "retries": b["retries"],
            "avg_retries": round(b["retries"] / b["requests"], 2) if b["requests"] else 0,
            "succeeded": b["succeeded"],
            "failed": b["fail"],
            "availability_pct": round(b["succeeded"] / b["requests"] * 100, 2) if b["requests"] else 0,
            "upstream_availability_pct": round(b["first_ok"] / b["requests"] * 100, 2) if b["requests"] else 0,
            "avg_duration": round(sum(ds) / n, 3) if n else 0,
            "p95_duration": round(_percentile(ds, 0.95), 3) if n else 0,
            "max_retries": b["max_retries"],
        })
    return out


def compute_stats(records: list, range_str: str = "today") -> dict:
    total = len(records)
    total_retries = sum(r.get("retries", 0) for r in records)
    succeeded = sum(1 for r in records if _req_succeeded(r))
    upstream_ok = sum(1 for r in records if _req_succeeded(r) and r.get("first_ok", r.get("retries", 0) == 0))
    failed = total - succeeded
    avail = round(succeeded / total * 100, 2) if total else 0
    upstream_avail = round(upstream_ok / total * 100, 2) if total else 0

    dist = Counter(r.get("retries", 0) for r in records)
    retry_distribution = [{"retries": k, "count": v} for k, v in sorted(dist.items())]

    bm = {"none": 0, "light": 0, "medium": 0, "heavy": 0, "severe": 0}
    for r in records:
        rt = r.get("retries", 0)
        if rt == 0:
            bm["none"] += 1
        elif rt <= 5:
            bm["light"] += 1
        elif rt <= 20:
            bm["medium"] += 1
        elif rt <= 50:
            bm["heavy"] += 1
        else:
            bm["severe"] += 1
    retry_burden = [
        {"bucket": "0 次", "count": bm["none"]},
        {"bucket": "1-5 次", "count": bm["light"]},
        {"bucket": "6-20 次", "count": bm["medium"]},
        {"bucket": "21-50 次", "count": bm["heavy"]},
        {"bucket": ">50 次", "count": bm["severe"]},
    ]

    # 时间桶精度：当天精确到分钟，7天/30天精确到小时，全部精确到天
    if range_str == "today":
        ts_slice = 16  # "2026-07-07T12:30"
    elif range_str in ("7d", "30d"):
        ts_slice = 13  # "2026-07-07T12"
    else:
        ts_slice = 10  # "2026-07-07"

    tl = defaultdict(lambda: {"requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "failed": 0})
    for r in records:
        ts = r.get("ts", "")
        bucket = ts[:ts_slice] if len(ts) >= ts_slice else ts
        b = tl[bucket]
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            b["succeeded"] += 1
            if r.get("first_ok", r.get("retries", 0) == 0):
                b["first_ok"] += 1
        else:
            b["failed"] += 1
    timeline = []
    for b, s in sorted(tl.items()):
        timeline.append({
            "ts": b, "requests": s["requests"], "retries": s["retries"],
            "succeeded": s["succeeded"], "failed": s["failed"],
            "availability_pct": round(s["succeeded"] / s["requests"] * 100, 2) if s["requests"] else 0,
            "upstream_availability_pct": round(s["first_ok"] / s["requests"] * 100, 2) if s["requests"] else 0,
        })

    upstream_sc = Counter()
    error_sc = Counter()
    for r in records:
        us = r.get("upstream_status", 0)
        upstream_sc[us] += 1
        rc = r.get("retry_codes")
        if rc:
            for code in rc:
                upstream_sc[code] += 1
                error_sc[code] += 1
        else:
            if us == 0 or us >= 400:
                error_sc[us] += 1
    final_sc = Counter(r.get("final_status", 0) for r in records)
    upstream_status_codes = [{"status": k, "count": v, "category": _status_category(k)}
                             for k, v in sorted(upstream_sc.items(), key=lambda x: -x[1])]
    upstream_error_codes = [{"status": k, "count": v, "category": _status_category(k)}
                            for k, v in sorted(error_sc.items(), key=lambda x: -x[1])]
    final_status_codes = [{"status": k, "count": v, "category": _status_category(k)}
                          for k, v in sorted(final_sc.items(), key=lambda x: -x[1])]

    status_categories = [{"status": k, "count": v}
                         for k, v in sorted(upstream_sc.items(), key=lambda x: -x[1])]

    model_status = defaultdict(Counter)
    for r in records:
        m = _model_key(r)
        # 统计所有上游响应：重试过程中的错误码 + 最终上游状态码
        rc = r.get("retry_codes")
        if rc:
            for code in rc:
                model_status[m][code] += 1
        model_status[m][r.get("upstream_status", 0)] += 1
    all_codes = sorted(set(code for c in model_status.values() for code in c))
    status_by_model = []
    for m, c in model_status.items():
        entry = {"model": m, "total": sum(c.values())}
        for code in all_codes:
            entry[str(code)] = c.get(code, 0)
        status_by_model.append(entry)
    status_by_model.sort(key=lambda x: x["total"], reverse=True)

    # 失败原因（上游错误码）：统计所有上游返回的错误码。
    # 优先取 retry_codes（重试过程中遇到的 503/429 等），
    # 没有时取 upstream_status（仅当它是错误码 >=400 或 0）。
    # 这样即使代理重试有效、请求最终成功，也能看到上游错误。
    fail_causes = Counter()
    for r in records:
        rc = r.get("retry_codes")
        if rc:
            for code in rc:
                fail_causes[code] += 1
        else:
            us = r.get("upstream_status", 0)
            if us == 0 or us >= 400:
                fail_causes[us] += 1
    failure_causes = [{"status": k, "count": v, "category": _status_category(k)}
                      for k, v in fail_causes.most_common()]

    worst_streak = 0
    cur_streak = 0
    cur_type = None
    for r in records:
        ok = _req_succeeded(r)
        if ok:
            if cur_type == "success":
                cur_streak += 1
            else:
                cur_type, cur_streak = "success", 1
        else:
            if cur_type == "failure":
                cur_streak += 1
            else:
                cur_type, cur_streak = "failure", 1
            worst_streak = max(worst_streak, cur_streak)

    durations = sorted([r["duration_s"] for r in records if isinstance(r.get("duration_s"), (int, float))])
    dn = len(durations)
    duration_stats = {
        "avg": round(sum(durations) / dn, 3) if dn else 0,
        "p50": round(_percentile(durations, 0.5), 3) if dn else 0,
        "p95": round(_percentile(durations, 0.95), 3) if dn else 0,
        "p99": round(_percentile(durations, 0.99), 3) if dn else 0,
        "max": round(durations[-1], 3) if dn else 0,
    }
    slowest = sorted(records, key=lambda r: r.get("duration_s", 0), reverse=True)[:8]
    slowest_requests = [{
        "ts": r.get("ts", ""), "model": _model_key(r), "path": r.get("path", ""),
        "retries": r.get("retries", 0), "duration_s": round(r.get("duration_s", 0), 2),
        "upstream_status": r.get("upstream_status", 0), "succeeded": _req_succeeded(r),
    } for r in slowest]

    fastest = sorted(
        [r for r in records if _req_succeeded(r)
         and isinstance(r.get("duration_s"), (int, float)) and r.get("duration_s", 0) > 0],
        key=lambda r: r["duration_s"],
    )[:8]
    fastest_requests = [{
        "ts": r.get("ts", ""), "model": _model_key(r), "path": r.get("path", ""),
        "retries": r.get("retries", 0), "duration_s": round(r.get("duration_s", 0), 2),
        "upstream_status": r.get("upstream_status", 0), "succeeded": _req_succeeded(r),
    } for r in fastest]

    hour_buckets = defaultdict(lambda: {"requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0})
    for r in records:
        ts = r.get("ts", "")
        try:
            h = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        hour_buckets[h]["requests"] += 1
        hour_buckets[h]["retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            hour_buckets[h]["succeeded"] += 1
            if r.get("first_ok", r.get("retries", 0) == 0):
                hour_buckets[h]["first_ok"] += 1
    by_hour = []
    for h in range(24):
        s = hour_buckets.get(h, {"requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0})
        by_hour.append({
            "hour": h, "requests": s["requests"], "retries": s["retries"],
            "availability_pct": round(s["succeeded"] / s["requests"] * 100, 2) if s["requests"] else 0,
            "upstream_availability_pct": round(s["first_ok"] / s["requests"] * 100, 2) if s["requests"] else 0,
        })

    by_path = _agg_by(records, "path", "path")
    by_path.sort(key=lambda x: x["retries"], reverse=True)
    by_path = by_path[:10]

    return {
        "summary": {
            "total_requests": total,
            "total_retries": total_retries,
            "avg_retries": round(total_retries / total, 2) if total else 0,
            "success_rate": round(succeeded / total, 4) if total else 0,
            "availability_pct": avail,
            "upstream_availability_pct": upstream_avail,
            "failed_requests": failed,
        },
        "by_provider": _agg_by(records, "provider", "provider"),
        "by_model": [m for m in _agg_by(records, "model", "model", key_fn=_model_key) if not m["model"].endswith("/(unknown)")],
        "by_key": [k for k in _agg_by(records, "key_id", "key_id") if k["key_id"] != "(unknown)"],
        "retry_distribution": retry_distribution,
        "retry_burden": retry_burden,
        "timeline": timeline,
        "upstream_status_codes": upstream_status_codes,
        "upstream_error_codes": upstream_error_codes,
        "final_status_codes": final_status_codes,
        "status_categories": status_categories,
        "status_by_model": status_by_model,
        "failure_causes": failure_causes,
        "availability": {
            "overall_pct": avail,
            "upstream_pct": upstream_avail,
            "worst_failure_streak": worst_streak,
            "current_streak": cur_streak,
            "current_streak_type": cur_type or "none",
            "failed_count": failed,
        },
        "duration_stats": duration_stats,
        "slowest_requests": slowest_requests,
        "fastest_requests": fastest_requests,
        "by_hour": by_hour,
        "by_path": by_path,
        "mode_comparison": _mode_comparison(records),
        "config": {
            "provider": PROVIDER,
            "upstream_url": UPSTREAM_URL,
            "routes": [
                {"prefix": (p or "/"), "upstream": u, "provider": pv}
                for p, u, pv, _ in ROUTES
            ],
            "retry_status_codes": sorted(RETRY_STATUS_CODES),
            "retry_interval": RETRY_INTERVAL,
            "retry_interval_429": RETRY_INTERVAL_429,
            "retry_backoff": RETRY_BACKOFF,
            "retry_backoff_max": RETRY_BACKOFF_MAX,
            "retry_backoff_429": RETRY_BACKOFF_429,
            "retry_backoff_max_429": RETRY_BACKOFF_MAX_429,
            "max_retries": MAX_RETRIES,
            "timeout": TIMEOUT,
        },
    }


@app.get("/health")
async def health():
    routes = [
        {"prefix": (p or "/"), "upstream": u, "provider": pv}
        for p, u, pv, _ in ROUTES
    ]
    key_pools = {
        url: {"provider": pool.provider or PROVIDER, "keys": pool.status()}
        for url, pool in KEY_POOLS.items()
    }
    return {"status": "ok", "upstream": UPSTREAM_URL, "routes": routes, "key_pools": key_pools}


@app.get("/stats")
async def stats_page():
    if os.path.exists(STATS_HTML_PATH):
        with open(STATS_HTML_PATH, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/html; charset=utf-8")
    return Response(content="stats.html not found", status_code=404)


def _summary_agg_view(d: dict) -> list:
    out = []
    for k, b in d.items():
        fo = b.get("first_ok", 0)
        out.append({
            "name": k, "requests": b["requests"], "retries": b["retries"],
            "avg_retries": round(b["retries"] / b["requests"], 2) if b["requests"] else 0,
            "availability_pct": round(b["succeeded"] / b["requests"] * 100, 2) if b["requests"] else 0,
            "upstream_availability_pct": round(fo / b["requests"] * 100, 2) if b["requests"] else 0,
            "max_retries": b["max_retries"],
        })
    out.sort(key=lambda x: x["requests"], reverse=True)
    return out


def _cumulative_view() -> dict:
    s = _summary_cache or _new_summary()
    total = s["total_requests"]
    return {
        "total_requests": total,
        "total_retries": s["total_retries"],
        "avg_retries": round(s["total_retries"] / total, 2) if total else 0,
        "availability_pct": round(s["total_succeeded"] / total * 100, 2) if total else 0,
        "upstream_availability_pct": round(s.get("total_first_ok", 0) / total * 100, 2) if total else 0,
        "succeeded": s["total_succeeded"],
        "failed": s["total_failed"],
        "by_provider": _summary_agg_view(s["by_provider"]),
        "by_model": _summary_agg_view({k: v for k, v in s["by_model"].items() if not k.endswith("/(unknown)")}),
        "by_key": _summary_agg_view(s.get("by_key", {})),
        "by_status": [{"status": k, "count": v} for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1])],
        "first_ts": s.get("first_ts"),
        "last_ts": s.get("last_ts"),
    }


def _count_succeeded_since(records: list, cutoff: datetime) -> int:
    """统计 cutoff 之后成功的请求数"""
    count = 0
    for r in records:
        ts = r.get("ts", "")
        try:
            t = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if t >= cutoff and _req_succeeded(r):
            count += 1
    return count


@app.get("/stats/api")
async def stats_api(range: str = "today", model: str = "", plan_start: str = "", rate_mode: str = ""):
    range_map = {"today": 1, "7d": 7, "30d": 30, "all": 0}
    days = range_map.get(range, 1)
    records = load_log_records(days)
    available_models = sorted(set(r.get("model", "") for r in records if r.get("model", "")))
    selected = set()
    if model:
        selected = {m.strip() for m in model.split(",") if m.strip()}
        records = [r for r in records if r.get("model", "") in selected]
    # 滑动窗口上游可用率：始终从近2天记录计算，不受所选时间范围影响
    window_records = load_log_records(2)
    if selected:
        window_records = [r for r in window_records if r.get("model", "") in selected]
    # 速率统计：5h/周/月的成功请求次数，独立加载30天记录
    rate_records = load_log_records(30)
    if selected:
        rate_records = [r for r in rate_records if r.get("model", "") in selected]
    now = datetime.now()
    ps_dt = None
    if plan_start:
        try:
            ps_dt = datetime.fromisoformat(plan_start)
        except (ValueError, TypeError):
            pass

    def _sliding():
        return (
            _count_succeeded_since(rate_records, now - timedelta(hours=5)),
            _count_succeeded_since(rate_records, now - timedelta(days=7)),
            _count_succeeded_since(rate_records, now - timedelta(days=30)),
        )

    if rate_mode == "platform" and ps_dt:
        # 平台流控：5h整点滑动/周每日08:00滑动/月固定31天
        whole_hour = now.replace(minute=0, second=0, microsecond=0)
        c5h = _count_succeeded_since(rate_records, max(whole_hour - timedelta(hours=5), ps_dt))
        today_0800 = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now < today_0800:
            today_0800 -= timedelta(days=1)
        c_week = _count_succeeded_since(rate_records, max(today_0800 - timedelta(days=7), ps_dt))
        c_month = _count_succeeded_since(rate_records, ps_dt)
    elif rate_mode == "fixed" and ps_dt:
        # 固定周期：从订阅时间按5h/7d/30d块对齐
        elapsed_days = max(0, (now - ps_dt).days)
        elapsed_5h = max(0, int((now - ps_dt).total_seconds() // 3600 // 5))
        c5h = _count_succeeded_since(rate_records, ps_dt + timedelta(hours=elapsed_5h * 5))
        c_week = _count_succeeded_since(rate_records, ps_dt + timedelta(days=(elapsed_days // 7) * 7))
        c_month = _count_succeeded_since(rate_records, ps_dt + timedelta(days=(elapsed_days // 30) * 30))
    else:
        # 滑动窗口（默认）
        c5h, c_week, c_month = _sliding()
    return {
        "detail": compute_stats(records, range),
        "cumulative": _cumulative_view(),
        "range": range,
        "record_count": len(records),
        "available_models": available_models,
        "upstream_windows": _upstream_window_stats(window_records),
        "rate_counts": {"5h": c5h, "week": c_week, "month": c_month},
    }


async def _race_request(method, url, req_headers, body, path, t0, provider, model, pool=None):
    """请求竞速：每轮一次性发 MAX_CONCURRENT 个，第一个成功立即取消其余。
    全部失败则等待间隔后下一轮，直到成功或达到 MAX_RETRIES。
    号池模式：每轮用当前最便宜可用 key，轮失败时冷却该 key 并立即换 key 下一轮（有可用 key 时跳过退避）。"""
    c = client
    assert c is not None
    total_sent = 0
    last_status = 0
    round_num = 0
    retry_codes = []
    consecutive_429_rounds = 0
    consecutive_non429_rounds = 0
    last_key_id = ""

    async def do_send(attempt_num, hdrs):
        response: Optional[httpx.Response] = None
        try:
            req = c.build_request(method, url, headers=hdrs, content=body if body else None)
            response = await c.send(req, stream=True)
            return ("ok", response, attempt_num)
        except Exception as e:
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            if isinstance(e, asyncio.CancelledError):
                raise
            return ("error", e, attempt_num)

    while True:
        round_num += 1
        to_fire = min(MAX_CONCURRENT, MAX_RETRIES - total_sent) if MAX_RETRIES > 0 else MAX_CONCURRENT
        if to_fire <= 0:
            break

        # 号池：每轮选当前最便宜可用 key
        round_key_entry = None
        round_host_error = False
        round_hdrs = req_headers
        if pool is not None:
            round_key_entry = pool.pick()
            if round_key_entry is not None:
                round_hdrs = _headers_with_key(req_headers, round_key_entry.key)
                last_key_id = round_key_entry.key_id

        batch_start = total_sent
        tasks = set()
        for _ in range(to_fire):
            total_sent += 1
            tasks.add(asyncio.create_task(do_send(total_sent, round_hdrs)))

        key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
        logger.info(
            f"{_tag(method, path, provider, model)}{key_tag} R{round_num} {to_fire}发(#{batch_start + 1}-#{total_sent}) {time.time() - t0:.1f}s"
        )

        winner = None
        winner_attempt = 0
        to_close = []
        saw_429 = False
        saw_429_wait = 0.0
        remaining = tasks

        while remaining and winner is None:
            done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                kind, result, attempt_num = task.result()

                if kind == "error":
                    last_status = 0
                    retry_codes.append(0)
                    if _is_host_level_error(result):
                        round_host_error = True
                    logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt_num}({time.time() - t0:.1f}s): {result!r}")
                elif _should_retry_status(result.status_code):
                    last_status = result.status_code
                    retry_codes.append(result.status_code)
                    if result.status_code == 429:
                        saw_429 = True
                        ra_header = result.headers.get("retry-after")
                        w = parse_retry_after(ra_header)
                        if w is not None:
                            saw_429_wait = max(saw_429_wait, w)
                        logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(429)} #{attempt_num}({time.time() - t0:.1f}s)")
                    else:
                        logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt_num}({time.time() - t0:.1f}s)")
                    to_close.append(result)
                else:
                    winner = result
                    winner_attempt = attempt_num
                    last_status = result.status_code

        for t in remaining:
            t.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        for t in remaining:
            if t.done() and not t.cancelled():
                try:
                    k, r, _ = t.result()
                    if k == "ok":
                        to_close.append(r)
                except Exception:
                    pass

        for r in to_close:
            if r is winner:
                continue
            try:
                await r.aread()
            except Exception:
                pass
            try:
                await r.aclose()
            except Exception:
                pass

        if winner is not None:
            logger.info(
                f"{_tag(method, path, provider, model)}{key_tag} -> {_sc(winner.status_code)} #{winner_attempt}胜出(R{round_num},{total_sent}发) {time.time() - t0:.2f}s"
            )
            return winner, winner_attempt, total_sent, last_status, retry_codes, (round_num == 1), last_key_id

        if MAX_RETRIES > 0 and total_sent >= MAX_RETRIES:
            break

        # 号池：冷却当前 key，有可用 key 则立即换 key 下一轮（跳过退避）
        # 连接级错误(DNS/连接失败/超时)与 key 无关，不冷却 key，走正常退避
        if pool is not None and round_key_entry is not None and not round_host_error:
            pool.mark_cooldown(round_key_entry, KEY_COOLDOWN, saw_429_wait if saw_429_wait > 0 else None)
            if pool.has_fresh():
                logger.info(f"{_tag(method, path, provider, model)}{key_tag} R{round_num}全败 换key {time.time() - t0:.1f}s")
                continue

        if saw_429:
            consecutive_429_rounds += 1
            consecutive_non429_rounds = 0
            ra_wait = saw_429_wait if saw_429_wait > 0 else None
            wait, wait_src = _calc_backoff_wait(consecutive_429_rounds, RETRY_INTERVAL_429, RETRY_BACKOFF_MAX_429, RETRY_BACKOFF_429, ra_wait)
        else:
            consecutive_429_rounds = 0
            consecutive_non429_rounds += 1
            wait, wait_src = _calc_backoff_wait(consecutive_non429_rounds, RETRY_INTERVAL, RETRY_BACKOFF_MAX, RETRY_BACKOFF)
        logger.info(
            f"{_tag(method, path, provider, model)}{key_tag} R{round_num}全败 {wait:.1f}s后{f'({wait_src})' if wait_src else ''} {time.time() - t0:.1f}s"
        )
        await asyncio.sleep(wait)

    return None, 0, total_sent, last_status, retry_codes, False, last_key_id


async def _hedge_request(method, url, req_headers, body, path, t0, provider, model, pool=None):
    """滚动竞速：按 RETRY_INTERVAL 交错发请求，非429错误立即补发（或按指数退避延迟），
    任意一个返回成功即取消其余。返回 (winner, winner_attempt, total_sent, last_status, retry_codes, first_ok, key_id)。
    号池模式：每次发请求选当前最便宜可用 key，失败时冷却该 key，下次自动选下一个。时序逻辑不变。"""
    c = client
    assert c is not None
    total_sent = 0
    last_status = 0
    retry_codes = []
    in_flight: dict = {}
    winner = None
    winner_attempt = 0
    next_fire_allowed = 0.0
    consecutive_429 = 0
    consecutive_non429 = 0
    last_key_id = ""

    async def do_send(attempt_num):
        nonlocal last_key_id
        entry = None
        hdrs = req_headers
        if pool is not None:
            entry = pool.pick()
            if entry is not None:
                hdrs = _headers_with_key(req_headers, entry.key)
                last_key_id = entry.key_id
        response: Optional[httpx.Response] = None
        try:
            req = c.build_request(method, url, headers=hdrs, content=body if body else None)
            response = await c.send(req, stream=True)
            return ("ok", response, attempt_num, entry)
        except Exception as e:
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            if isinstance(e, asyncio.CancelledError):
                raise
            return ("error", e, attempt_num, entry)

    def can_fire(now):
        return (
            winner is None
            and (MAX_RETRIES == 0 or total_sent < MAX_RETRIES)
            and len(in_flight) < MAX_CONCURRENT
            and now >= next_fire_allowed
        )

    total_sent += 1
    task = asyncio.create_task(do_send(total_sent))
    in_flight[task] = time.time()

    while True:
        if winner is not None:
            break

        if not in_flight:
            if MAX_RETRIES > 0 and total_sent >= MAX_RETRIES:
                break
            now = time.time()
            wait = max(next_fire_allowed - now, 0.0)
            if wait > 0:
                key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
                logger.info(f"{_tag(method, path, provider, model)}{key_tag} 退避 {wait:.1f}s {now - t0:.1f}s")
                await asyncio.sleep(wait)
            now = time.time()
            if can_fire(now):
                total_sent += 1
                task = asyncio.create_task(do_send(total_sent))
                in_flight[task] = now
            continue

        now = time.time()
        oldest_fire = min(in_flight.values())
        stagger_delay = max(oldest_fire + RETRY_INTERVAL - now, 0.0)

        done, _ = await asyncio.wait(
            set(in_flight.keys()),
            timeout=stagger_delay,
            return_when=asyncio.FIRST_COMPLETED,
        )

        now = time.time()

        if not done:
            if can_fire(now):
                total_sent += 1
                new_task = asyncio.create_task(do_send(total_sent))
                in_flight[new_task] = now
                key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
                logger.info(f"{_tag(method, path, provider, model)}{key_tag} 补发#{total_sent}(在飞{len(in_flight)}) {now - t0:.1f}s")
            continue

        for task in done:
            in_flight.pop(task, None)
            if task.cancelled():
                continue
            kind, result, attempt_num, key_entry = task.result()
            key_tag = f"[{key_entry.key_id}]" if (pool is not None and key_entry is not None) else ""

            if kind == "error":
                last_status = 0
                retry_codes.append(0)
                logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt_num}({now - t0:.1f}s) {result!r} 立即补发")
                # 连接级错误与 key 无关，不冷却 key
                if pool is not None and key_entry is not None and not _is_host_level_error(result):
                    pool.mark_cooldown(key_entry, KEY_COOLDOWN)
                if can_fire(now):
                    total_sent += 1
                    new_task = asyncio.create_task(do_send(total_sent))
                    in_flight[new_task] = now

            elif _should_retry_status(result.status_code):
                last_status = result.status_code
                retry_codes.append(result.status_code)
                # 号池：冷却该 key，下次 do_send 自动选下一个可用 key
                if pool is not None and key_entry is not None:
                    ra_w = parse_retry_after(result.headers.get("retry-after")) if result.status_code == 429 else None
                    pool.mark_cooldown(key_entry, KEY_COOLDOWN, ra_w)
                if result.status_code == 429:
                    consecutive_429 += 1
                    consecutive_non429 = 0
                    ra_wait = parse_retry_after(result.headers.get("retry-after"))
                    wait, wait_src = _calc_backoff_wait(consecutive_429, RETRY_INTERVAL_429, RETRY_BACKOFF_MAX_429, RETRY_BACKOFF_429, ra_wait)
                    next_fire_allowed = max(next_fire_allowed, now + wait)
                    logger.warning(
                        f"{_tag(method, path, provider, model)}{key_tag} {_sc(429)} #{attempt_num} {wait:.1f}s{f'({wait_src})' if wait_src else ''} "
                        f"在飞{len(in_flight)} {now - t0:.1f}s"
                    )
                else:
                    consecutive_429 = 0
                    consecutive_non429 += 1
                    if RETRY_BACKOFF:
                        wait, wait_src = _calc_backoff_wait(consecutive_non429, RETRY_INTERVAL, RETRY_BACKOFF_MAX, RETRY_BACKOFF)
                        next_fire_allowed = max(next_fire_allowed, now + wait)
                        logger.warning(
                            f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt_num} {wait:.1f}s{f'({wait_src})' if wait_src else ''} "
                            f"在飞{len(in_flight)} {now - t0:.1f}s"
                        )
                    else:
                        logger.warning(
                            f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt_num} 立即补发 "
                            f"在飞{len(in_flight)} {now - t0:.1f}s"
                        )
                        if can_fire(now):
                            total_sent += 1
                            new_task = asyncio.create_task(do_send(total_sent))
                            in_flight[new_task] = now
                try:
                    await result.aread()
                except Exception:
                    pass
                await result.aclose()

            else:
                winner = result
                winner_attempt = attempt_num
                last_status = result.status_code
                last_key_id = key_entry.key_id if key_entry is not None else ""
                logger.info(
                    f"{_tag(method, path, provider, model)}{key_tag} -> {_sc(result.status_code)} #{attempt_num}胜出({total_sent}发) {now - t0:.2f}s"
                )
                break

        if winner is not None:
            for t in list(in_flight.keys()):
                if not t.done():
                    t.cancel()
            pending_tasks = [t for t in list(in_flight.keys()) if not t.done()]
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            for t in list(in_flight.keys()):
                if t.done() and not t.cancelled():
                    try:
                        k, r, _, _ = t.result()
                        if k == "ok" and r is not winner:
                            await r.aclose()
                    except Exception:
                        pass
            in_flight.clear()
            break

        now = time.time()
        if (
            in_flight
            and can_fire(now)
            and any(now - ft >= RETRY_INTERVAL for ft in in_flight.values())
        ):
            total_sent += 1
            new_task = asyncio.create_task(do_send(total_sent))
            in_flight[new_task] = now
            key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
            logger.info(f"{_tag(method, path, provider, model)}{key_tag} 补发#{total_sent}(在飞{len(in_flight)}) {now - t0:.1f}s")

    return winner, winner_attempt, total_sent, last_status, retry_codes, (winner is not None and winner_attempt == 1), last_key_id


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy(path: str, request: Request):
    if _is_excluded_path(path):
        return Response(status_code=404)
    method = request.method
    upstream_url, provider, remaining = match_route(path)
    url = f"{upstream_url}/{remaining}" if remaining else upstream_url
    if request.url.query:
        url += f"?{request.url.query}"

    body = await request.body() if method not in ("GET", "HEAD") else b""
    req_headers = filter_headers(request.headers, SKIP_REQUEST_HEADERS)
    model = parse_model(body)

    # 号池查找：按 match_route 返回的 upstream_url 匹配对应的 key 池
    pool = KEY_POOLS.get(upstream_url)

    t0 = time.time()

    if HEDGE_MODE in ("race", "stagger"):
        if HEDGE_MODE == "race":
            winner, winner_attempt, total_sent, last_status, retry_codes, first_ok, key_id = await _race_request(
                method, url, req_headers, body, path, t0, provider, model, pool
            )
        else:
            winner, winner_attempt, total_sent, last_status, retry_codes, first_ok, key_id = await _hedge_request(
                method, url, req_headers, body, path, t0, provider, model, pool
            )
        if winner is not None:
            resp_headers = filter_headers(winner.headers, SKIP_RESPONSE_HEADERS)
            resp_headers["X-Forward-Attempts"] = str(winner_attempt)
            content_type = winner.headers.get("content-type")
            status = winner.status_code

            await write_log({
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "method": method,
                "path": "/" + path,
                "provider": provider,
                "model": model,
                "upstream_status": last_status,
                "final_status": status,
                "attempts": total_sent,
                "retries": max(total_sent - 1, 0),
                "duration_s": round(time.time() - t0, 3),
                "succeeded": status < 400,
                "retry_codes": retry_codes,
                "mode": HEDGE_MODE,
                "first_ok": first_ok,
                "key_id": key_id,
            })

            async def hedge_body_gen():
                try:
                    async for chunk in winner.aiter_bytes():
                        yield chunk
                finally:
                    await winner.aclose()

            return StreamingResponse(
                hedge_body_gen(),
                status_code=status,
                headers=resp_headers,
                media_type=content_type,
            )
        else:
            logger.error(
                f"{_tag(method, path, provider, model)} 放弃({total_sent}发) {time.time()-t0:.1f}s"
            )
            await write_log({
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "method": method,
                "path": "/" + path,
                "provider": provider,
                "model": model,
                "upstream_status": last_status,
                "final_status": 503,
                "attempts": total_sent,
                "retries": max(total_sent - 1, 0),
                "duration_s": round(time.time() - t0, 3),
                "succeeded": False,
                "retry_codes": retry_codes,
                "mode": HEDGE_MODE,
                "first_ok": first_ok,
                "key_id": key_id,
            })
            return Response(
                content=(
                    f'{{"error":{{"message":"upstream overloaded after {total_sent} attempts",'
                    f'"type":"upstream_error","code":"503"}}}}'
                ),
                status_code=503,
                media_type="application/json",
                headers={"X-Forward-Attempts": str(total_sent)},
            )

    attempt = 0
    last_status = 0
    retry_codes = []
    consecutive_429 = 0
    consecutive_non429 = 0
    last_key_id = ""

    while True:
        attempt += 1

        # 号池：选当前最便宜可用 key
        current_key_entry = None
        send_headers = req_headers
        if pool is not None:
            current_key_entry = pool.pick()
            if current_key_entry is not None:
                send_headers = _headers_with_key(req_headers, current_key_entry.key)
                last_key_id = current_key_entry.key_id
        key_tag = f"[{last_key_id}]" if (pool is not None and last_key_id) else ""

        if MAX_RETRIES > 0 and attempt > MAX_RETRIES:
            total = attempt - 1
            logger.error(
                f"{_tag(method, path, provider, model)}{key_tag} 放弃({MAX_RETRIES}次) {time.time()-t0:.1f}s"
            )
            await write_log({
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "method": method,
                "path": "/" + path,
                "provider": provider,
                "model": model,
                "upstream_status": last_status,
                "final_status": 503,
                "attempts": total,
                "retries": max(total - 1, 0),
                "duration_s": round(time.time() - t0, 3),
                "succeeded": False,
                "retry_codes": retry_codes,
                "mode": HEDGE_MODE,
                "first_ok": False,
                "key_id": last_key_id,
            })
            return Response(
                content=(
                    f'{{"error":{{"message":"upstream overloaded after {MAX_RETRIES} retries",'
                    f'"type":"upstream_error","code":"503"}}}}'
                ),
                status_code=503,
                media_type="application/json",
                headers={"X-Forward-Attempts": str(total)},
            )

        cycle_start = time.time()
        try:
            assert client is not None
            req = client.build_request(
                method,
                url,
                headers=send_headers,
                content=body if body else None,
            )
            response = await client.send(req, stream=True)
        except (httpx.RequestError, httpx.HTTPError) as e:
            last_status = 0
            retry_codes.append(0)
            elapsed = time.time() - cycle_start
            sleep_for = max(RETRY_INTERVAL - elapsed, 0.0)
            host_err = _is_host_level_error(e)
            logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt}({elapsed:.2f}s) {e!r} {sleep_for:.2f}s后重试")
            # 连接级错误(DNS/连接失败/超时)与 key 无关，不冷却 key 不换 key，直接退避重试同一 key
            if pool is not None and current_key_entry is not None and not host_err:
                pool.mark_cooldown(current_key_entry, KEY_COOLDOWN)
                # 号池：有可用 key 则立即换 key 重试，跳过等待
                if pool.has_fresh():
                    logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt} 换key 总{time.time()-t0:.1f}s")
                    continue
            await asyncio.sleep(sleep_for)
            continue

        if _should_retry_status(response.status_code):
            last_status = response.status_code
            retry_codes.append(response.status_code)
            ra_wait = parse_retry_after(response.headers.get("retry-after")) if response.status_code == 429 else None
            # 号池：冷却当前 key
            if pool is not None and current_key_entry is not None:
                pool.mark_cooldown(current_key_entry, KEY_COOLDOWN, ra_wait)
            try:
                await response.aread()
            except Exception:
                pass
            await response.aclose()
            # 号池：有可用 key 则立即换 key 重试，跳过退避（不递增计数器）
            if pool is not None and pool.has_fresh():
                logger.warning(
                    f"{_tag(method, path, provider, model)}{key_tag} {_sc(response.status_code)} #{attempt} 换key 总{time.time()-t0:.1f}s"
                )
                continue
            # 429 使用指数退避（优先 Retry-After，取 max(RA, backoff)）；其它状态码可选指数退避（RETRY_BACKOFF）
            if response.status_code == 429:
                consecutive_429 += 1
                consecutive_non429 = 0
                wait, wait_src = _calc_backoff_wait(consecutive_429, RETRY_INTERVAL_429, RETRY_BACKOFF_MAX_429, RETRY_BACKOFF_429, ra_wait)
            else:
                consecutive_429 = 0
                consecutive_non429 += 1
                wait, wait_src = _calc_backoff_wait(consecutive_non429, RETRY_INTERVAL, RETRY_BACKOFF_MAX, RETRY_BACKOFF)
            elapsed = time.time() - cycle_start
            # Retry-After / 退避值是"从响应起等待"，不扣 elapsed；固定间隔是"最小周期"，扣 elapsed
            if wait_src.startswith("RA"):
                sleep_for = wait
            else:
                sleep_for = max(wait - elapsed, 0.0)
            logger.warning(
                f"{_tag(method, path, provider, model)}{key_tag} {_sc(response.status_code)} #{attempt} "
                f"{sleep_for:.2f}s后重试{f'({wait_src})' if wait_src else ''} 总{time.time()-t0:.1f}s"
            )
            await asyncio.sleep(sleep_for)
            continue

        resp_headers = filter_headers(response.headers, SKIP_RESPONSE_HEADERS)
        resp_headers["X-Forward-Attempts"] = str(attempt)
        content_type = response.headers.get("content-type")
        status = response.status_code
        last_status = status

        logger.info(f"{_tag(method, path, provider, model)}{key_tag} -> {_sc(status)} #{attempt} {time.time()-t0:.2f}s")

        await write_log({
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "method": method,
            "path": "/" + path,
            "provider": provider,
            "model": model,
            "upstream_status": last_status,
            "final_status": status,
            "attempts": attempt,
            "retries": max(attempt - 1, 0),
            "duration_s": round(time.time() - t0, 3),
            "succeeded": status < 400,
            "retry_codes": retry_codes,
            "mode": HEDGE_MODE,
            "first_ok": (attempt == 1),
            "key_id": last_key_id,
        })

        async def body_gen():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except httpx.TransportError as e:
                # 上游在流式传输中途断连(incomplete chunked read 等);
                # 响应头已发送,无法重试/改状态码,只能记告警并优雅结束流
                logger.warning(
                    f"{_tag(method, path, provider, model)}{key_tag} 流式中断 #{attempt} {e!r} 总{time.time()-t0:.2f}s"
                )
            finally:
                await response.aclose()

        return StreamingResponse(
            body_gen(),
            status_code=status,
            headers=resp_headers,
            media_type=content_type,
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
