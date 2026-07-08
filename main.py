import os
import sys
import asyncio
import logging
import time
import json
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

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("forward")

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2").rstrip("/")
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))
RETRY_INTERVAL = float(os.getenv("RETRY_INTERVAL", "1.0"))
RETRY_INTERVAL_429 = float(os.getenv("RETRY_INTERVAL_429", "5.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "60"))
RETRY_STATUS_CODES = set(
    int(x) for x in os.getenv("RETRY_STATUS_CODES", "503,502,504,529,429").split(",") if x.strip()
)
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
        "by_provider": {}, "by_model": {}, "by_status": {},
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
            for k in ("by_provider", "by_model", "by_status"):
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
        if r.get("retries", 0) == 0:
            summary["total_first_ok"] = summary.get("total_first_ok", 0) + 1
    else:
        summary["total_failed"] += 1
    ts = r.get("ts")
    if ts:
        if not summary["first_ts"]:
            summary["first_ts"] = ts
        summary["last_ts"] = ts
    for fkey, k in (("by_provider", _normalize_provider(r.get("provider", "") or "(unknown)")),
                    ("by_model", _model_key(r))):
        b = summary[fkey].setdefault(k, {
            "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "failed": 0, "max_retries": 0,
        })
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        if _req_succeeded(r):
            b["succeeded"] += 1
            if r.get("retries", 0) == 0:
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
        if prefix:
            logger.info(f"  路由: {prefix}/* -> {upstream_url}/  (provider={provider}, 去前缀)")
        else:
            logger.info(f"  路由: /* -> {upstream_url}  (provider={provider}, 默认)")
    retry_desc = f"无限" if MAX_RETRIES <= 0 else str(MAX_RETRIES)
    mode_desc = {"off": "串行重试", "race": "请求竞速(一次并发)", "stagger": "滚动竞速(交错补发)"}.get(HEDGE_MODE, HEDGE_MODE)
    logger.info(f"重试: 间隔={RETRY_INTERVAL}s, 429间隔={RETRY_INTERVAL_429}s(优先Retry-After), 最大次数={retry_desc}, 状态码={sorted(RETRY_STATUS_CODES)}")
    logger.info(f"模式: {mode_desc}" + (f", 最大并发={MAX_CONCURRENT}" if HEDGE_MODE != "off" else ""))
    logger.info(f"记录: provider={PROVIDER}, 日志目录={LOG_DIR}, 保留{LOG_RETENTION_DAYS}天")
    logger.info(f"代理: trust_env={'是(跟随系统代理)' if TRUST_ENV else '否(直连)'}")
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


async def write_log(record: dict):
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
            if r.get("retries", 0) == 0:
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


def compute_stats(records: list, range_str: str = "today") -> dict:
    total = len(records)
    total_retries = sum(r.get("retries", 0) for r in records)
    succeeded = sum(1 for r in records if _req_succeeded(r))
    upstream_ok = sum(1 for r in records if _req_succeeded(r) and r.get("retries", 0) == 0)
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
            if r.get("retries", 0) == 0:
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
            if r.get("retries", 0) == 0:
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
        "by_model": _agg_by(records, "model", "model", key_fn=_model_key),
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
    return {"status": "ok", "upstream": UPSTREAM_URL, "routes": routes}


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
        "by_model": _summary_agg_view(s["by_model"]),
        "by_status": [{"status": k, "count": v} for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1])],
        "first_ts": s.get("first_ts"),
        "last_ts": s.get("last_ts"),
    }


@app.get("/stats/api")
async def stats_api(range: str = "today"):
    range_map = {"today": 1, "7d": 7, "30d": 30, "all": 0}
    days = range_map.get(range, 1)
    records = load_log_records(days)
    return {
        "detail": compute_stats(records, range),
        "cumulative": _cumulative_view(),
        "range": range,
        "record_count": len(records),
    }


async def _race_request(method, url, req_headers, body, path, t0):
    """请求竞速：每轮一次性发 MAX_CONCURRENT 个，第一个成功立即取消其余。
    全部失败则等待间隔后下一轮，直到成功或达到 MAX_RETRIES。"""
    c = client
    assert c is not None
    total_sent = 0
    last_status = 0
    round_num = 0
    retry_codes = []

    async def do_send(attempt_num):
        response: Optional[httpx.Response] = None
        try:
            req = c.build_request(method, url, headers=req_headers, content=body if body else None)
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

        batch_start = total_sent
        tasks = set()
        for _ in range(to_fire):
            total_sent += 1
            tasks.add(asyncio.create_task(do_send(total_sent)))

        logger.info(
            f"[{method} /{path}] 竞速第{round_num}轮，并发{to_fire}个(#{batch_start + 1}-#{total_sent})，"
            f"累计{total_sent}，{time.time() - t0:.1f}s"
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
                    logger.warning(f"[{method} /{path}] 竞速#{attempt_num}异常({time.time() - t0:.1f}s): {result!r}")
                elif result.status_code in RETRY_STATUS_CODES:
                    last_status = result.status_code
                    retry_codes.append(result.status_code)
                    if result.status_code == 429:
                        saw_429 = True
                        ra_header = result.headers.get("retry-after")
                        w = parse_retry_after(ra_header)
                        if w is not None:
                            saw_429_wait = max(saw_429_wait, w)
                        logger.warning(f"[{method} /{path}] 竞速#{attempt_num} 429({time.time() - t0:.1f}s)")
                    else:
                        logger.warning(f"[{method} /{path}] 竞速#{attempt_num} {result.status_code}({time.time() - t0:.1f}s)")
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
                f"[{method} /{path}] -> {winner.status_code}#{winner_attempt}竞速胜出"
                f"(第{round_num}轮，共发{total_sent}，{time.time() - t0:.2f}s)"
            )
            return winner, winner_attempt, total_sent, last_status, retry_codes

        if MAX_RETRIES > 0 and total_sent >= MAX_RETRIES:
            break

        if saw_429:
            wait = saw_429_wait if saw_429_wait > 0 else RETRY_INTERVAL_429
        else:
            wait = RETRY_INTERVAL
        logger.info(
            f"[{method} /{path}] 竞速第{round_num}轮全部失败，{wait:.1f}s后下一轮，"
            f"累计{time.time() - t0:.1f}s"
        )
        await asyncio.sleep(wait)

    return None, 0, total_sent, last_status, retry_codes


async def _hedge_request(method, url, req_headers, body, path, t0):
    """滚动竞速：按 RETRY_INTERVAL 交错发请求，非429错误立即补发，
    任意一个返回成功即取消其余。返回 (winner, winner_attempt, total_sent, last_status, retry_codes)。"""
    c = client
    assert c is not None
    total_sent = 0
    last_status = 0
    retry_codes = []
    in_flight: dict = {}
    winner = None
    winner_attempt = 0
    next_fire_allowed = 0.0

    async def do_send(attempt_num):
        response: Optional[httpx.Response] = None
        try:
            req = c.build_request(method, url, headers=req_headers, content=body if body else None)
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
                logger.info(f"[{method} /{path}] 429退避，等待{wait:.1f}s后继续，累计{now - t0:.1f}s")
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
                logger.info(f"[{method} /{path}] 交错补发#{total_sent}（在飞{len(in_flight)}），累计{now - t0:.1f}s")
            continue

        for task in done:
            in_flight.pop(task, None)
            if task.cancelled():
                continue
            kind, result, attempt_num = task.result()

            if kind == "error":
                last_status = 0
                retry_codes.append(0)
                logger.warning(f"[{method} /{path}] 请求异常#{attempt_num}({now - t0:.1f}s): {result!r}，立即补发")
                if can_fire(now):
                    total_sent += 1
                    new_task = asyncio.create_task(do_send(total_sent))
                    in_flight[new_task] = now

            elif result.status_code in RETRY_STATUS_CODES:
                last_status = result.status_code
                retry_codes.append(result.status_code)
                if result.status_code == 429:
                    ra_header = result.headers.get("retry-after")
                    wait = parse_retry_after(ra_header)
                    if wait is None:
                        wait = RETRY_INTERVAL_429
                        wait_src = "429-default"
                    else:
                        wait_src = "Retry-After"
                    next_fire_allowed = max(next_fire_allowed, now + wait)
                    logger.warning(
                        f"[{method} /{path}] 上游429#{attempt_num}，{wait:.1f}s后允许补发({wait_src})，"
                        f"在飞{len(in_flight)}，累计{now - t0:.1f}s"
                    )
                else:
                    logger.warning(
                        f"[{method} /{path}] 上游{result.status_code}#{attempt_num}，立即补发，"
                        f"在飞{len(in_flight)}，累计{now - t0:.1f}s"
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
                logger.info(
                    f"[{method} /{path}] -> {result.status_code}#{attempt_num}竞速胜出"
                    f"(共发{total_sent}，耗时{now - t0:.2f}s)"
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
                        k, r, _ = t.result()
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
            logger.info(f"[{method} /{path}] 交错补发#{total_sent}（在飞{len(in_flight)}），累计{now - t0:.1f}s")

    return winner, winner_attempt, total_sent, last_status, retry_codes


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

    t0 = time.time()

    if HEDGE_MODE in ("race", "stagger"):
        if HEDGE_MODE == "race":
            winner, winner_attempt, total_sent, last_status, retry_codes = await _race_request(
                method, url, req_headers, body, path, t0
            )
        else:
            winner, winner_attempt, total_sent, last_status, retry_codes = await _hedge_request(
                method, url, req_headers, body, path, t0
            )
        if winner is not None:
            resp_headers = filter_headers(winner.headers, SKIP_RESPONSE_HEADERS)
            resp_headers["X-Forward-Attempts"] = str(winner_attempt)
            content_type = winner.headers.get("content-type")
            status = winner.status_code

            logger.info(f"[{method} /{path}] -> {status} #{winner_attempt}竞速胜出 (共发{total_sent}，{time.time()-t0:.2f}s)")

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
                f"[{method} /{path}] 竞速耗尽(共发{total_sent})，放弃 (耗时 {time.time()-t0:.1f}s)"
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

    while True:
        attempt += 1

        if MAX_RETRIES > 0 and attempt > MAX_RETRIES:
            total = attempt - 1
            logger.error(
                f"[{method} /{path}] 达到最大重试次数 {MAX_RETRIES}，放弃 (耗时 {time.time()-t0:.1f}s)"
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
                headers=req_headers,
                content=body if body else None,
            )
            response = await client.send(req, stream=True)
        except (httpx.RequestError, httpx.HTTPError) as e:
            last_status = 0
            retry_codes.append(0)
            elapsed = time.time() - cycle_start
            sleep_for = max(RETRY_INTERVAL - elapsed, 0.0)
            logger.warning(f"[{method} /{path}] 请求异常 (attempt {attempt}, {elapsed:.2f}s): {e!r}，{sleep_for:.2f}s后重试")
            await asyncio.sleep(sleep_for)
            continue

        if response.status_code in RETRY_STATUS_CODES:
            last_status = response.status_code
            retry_codes.append(response.status_code)
            # 429 优先使用 Retry-After 头，其次用 RETRY_INTERVAL_429；其它状态码用 RETRY_INTERVAL
            if response.status_code == 429:
                ra_header = response.headers.get("retry-after")
                wait = parse_retry_after(ra_header)
                if wait is None:
                    wait = RETRY_INTERVAL_429
                    wait_src = "429-default"
                else:
                    wait_src = "Retry-After"
            else:
                wait = RETRY_INTERVAL
                wait_src = "default"
            try:
                await response.aread()
            except Exception:
                pass
            await response.aclose()
            elapsed = time.time() - cycle_start
            if wait_src == "Retry-After":
                sleep_for = wait
            else:
                sleep_for = max(wait - elapsed, 0.0)
            logger.warning(
                f"[{method} /{path}] 上游返回 {response.status_code} (attempt {attempt})，"
                f"本轮{elapsed:.2f}s，{sleep_for:.2f}s后重试({wait_src})，累计 {time.time()-t0:.1f}s"
            )
            await asyncio.sleep(sleep_for)
            continue

        resp_headers = filter_headers(response.headers, SKIP_RESPONSE_HEADERS)
        resp_headers["X-Forward-Attempts"] = str(attempt)
        content_type = response.headers.get("content-type")
        status = response.status_code
        last_status = status

        logger.info(f"[{method} /{path}] -> {status} (attempt {attempt}, {time.time()-t0:.2f}s)")

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
        })

        async def body_gen():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
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
    uvicorn.run("main:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
