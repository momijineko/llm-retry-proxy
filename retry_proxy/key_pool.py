import csv
import fnmatch
import hashlib
import os
import time
from typing import Optional

from .config import logger, settings


class KeyEntry:
    __slots__ = ("key", "key_id", "label", "models", "paths", "cooldown_until", "total_fail", "last_fail_ts")
    def __init__(self, key: str, label: str = "", models=(), paths=()):
        self.key, self.label = key, label
        self.key_id = label if label else key[:8]
        self.models = tuple(pattern.lower() for pattern in models)
        self.paths = tuple(pattern.lstrip("/").lower() for pattern in paths)
        self.cooldown_until = 0.0
        self.total_fail = 0
        self.last_fail_ts = 0.0


class KeyPool:
    def __init__(self, keys, provider: str = ""):
        self.entries = [KeyEntry(k[0], k[1] if len(k) > 1 else "") if isinstance(k, tuple) else KeyEntry(k) for k in keys]
        self.provider, self._current, self._sticky_until = provider, None, 0.0
        self._views = {}
        self.finalize_entries()

    def finalize_entries(self):
        unique = []
        seen_keys = set()
        for entry in self.entries:
            if entry.key in seen_keys:
                logger.warning(f"号池包含重复 key={entry.label or entry.key[:8]}，已去重")
                continue
            seen_keys.add(entry.key)
            unique.append(entry)
        self.entries = unique
        counts = {}
        for entry in self.entries:
            base = entry.label or entry.key[:8]
            counts[base] = counts.get(base, 0) + 1
        for entry in self.entries:
            base = entry.label or entry.key[:8]
            if counts[base] > 1:
                fingerprint = hashlib.sha256(entry.key.encode("utf-8")).hexdigest()[:8]
                entry.key_id = f"{base}#{fingerprint}"

    def for_request(self, model="", path=""):
        model = (model or "").lower()
        path = (path or "").lstrip("/").lower()
        matched = [entry for entry in self.entries if
                   (model and any(fnmatch.fnmatchcase(model, pattern) for pattern in entry.models)) or
                   (path and any(fnmatch.fnmatchcase(path, pattern) for pattern in entry.paths))]
        selected = matched or [entry for entry in self.entries if not entry.models and not entry.paths]
        if not selected:
            return None
        signature = tuple(id(entry) for entry in selected)
        if signature not in self._views:
            view = KeyPool([], self.provider)
            view.entries = selected
            self._views[signature] = view
        return self._views[signature]

    def pick(self):
        now = time.time()
        if self._current is not None and now < self._sticky_until and self._current.cooldown_until <= now:
            self._sticky_until = now + settings.key_sticky
            return self._current
        for entry in self.entries:
            if entry.cooldown_until <= now:
                self._current, self._sticky_until = entry, now + settings.key_sticky
                return entry
        best = min(self.entries, key=lambda e: e.cooldown_until) if self.entries else None
        if best is not None:
            self._current, self._sticky_until = best, now + settings.key_sticky
        return best

    def has_fresh(self):
        return any(e.cooldown_until <= time.time() for e in self.entries)

    def mark_cooldown(self, entry, seconds, ra_wait=None):
        now = time.time(); already = entry.cooldown_until > now
        entry.cooldown_until = now + max(seconds, ra_wait or 0.0)
        if not already: entry.total_fail += 1
        entry.last_fail_ts = now

    def status(self):
        now = time.time()
        return [{"key_id": e.key_id, "label": e.label, "cooled": e.cooldown_until > now,
                 "cooldown_remaining": round(max(e.cooldown_until - now, 0), 1), "total_fail": e.total_fail,
                 "models": list(e.models), "paths": list(e.paths)} for e in self.entries]


def _resolve_path(path):
    if os.path.isabs(path): return path if os.path.exists(path) else None
    if os.path.exists(path): return os.path.abspath(path)
    candidate = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    return candidate if os.path.exists(candidate) else None


def load_key_pools_csv(path):
    pools = {}; fpath = _resolve_path(path)
    if fpath is None:
        logger.warning(f"KEY_POOL_FILE 文件不存在: {path}"); return pools
    raw = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(fpath, encoding=enc) as f: raw = f.read()
            break
        except UnicodeDecodeError: pass
    if raw is None: logger.warning(f"KEY_POOL_FILE 编码无法识别(非 UTF-8/GBK): {fpath}"); return pools
    lines = [line for line in raw.splitlines() if line.strip().strip(",") and not line.strip().startswith("#")]
    if not lines:
        logger.warning(f"KEY_POOL_FILE 内容为空: {fpath}"); return pools
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        logger.warning(f"KEY_POOL_FILE 无表头，跳过: {fpath}"); return pools
    reader.fieldnames = [h.strip().lower() if h else h for h in reader.fieldnames]
    if "key" not in reader.fieldnames:
        logger.warning(f"KEY_POOL_FILE 缺少 key 列，跳过: {fpath}"); return pools
    for row in reader:
        key = (row.get("key") or "").strip()
        if not key: continue
        url = (row.get("url") or "").strip().rstrip("/") or settings.upstream_url
        provider = (row.get("provider") or "").strip() or settings.provider
        label = (row.get("label") or "").strip()
        models = tuple(pattern.strip() for pattern in (row.get("models") or "").split(";") if pattern.strip())
        paths = tuple(pattern.strip() for pattern in (row.get("paths") or "").split(";") if pattern.strip())
        if url in pools and provider and pools[url].provider != provider:
            logger.warning(f"号池 key={label or key[:8]} 的 provider={provider!r} 与池现有={pools[url].provider!r} 不一致，已忽略")
        pools.setdefault(url, KeyPool([], provider)).entries.append(KeyEntry(key, label, models, paths))
    if pools:
        for pool in pools.values():
            pool.finalize_entries()
        total = sum(len(p.entries) for p in pools.values())
        logger.info(f"号池CSV已加载: {fpath} ({len(pools)}个上游, 共{total}个key)")
    return pools


def build_key_pools():
    if settings.key_pool_file:
        pools = load_key_pools_csv(settings.key_pool_file)
        if pools: return pools
    pools = {}
    for group in settings.key_pools_raw.split(","):
        group = group.strip()
        if not group: continue
        if "|" in group:
            parts = group.split("|")
            if len(parts) < 3 or not parts[0].strip() or not parts[2].strip(): continue
            pools[parts[0].strip().rstrip("/")] = KeyPool([k.strip() for k in parts[2].split(";") if k.strip()], parts[1].strip())
        else:
            keys = [k.strip() for k in group.split(";") if k.strip()]
            if keys: pools[settings.upstream_url] = KeyPool(keys, settings.provider)
    if pools:
        total = sum(len(p.entries) for p in pools.values())
        logger.info(f"号池已加载: {len(pools)}个上游, 共{total}个key")
    return pools


KEY_POOLS = build_key_pools()
_AUTH_STRIP_HEADERS = {"authorization", settings.key_auth_header}


def headers_with_key(base_headers: dict, key: Optional[str]) -> dict:
    headers = {k: v for k, v in base_headers.items() if k.lower() not in _AUTH_STRIP_HEADERS}
    if key: headers[settings.key_auth_header] = f"{settings.key_auth_scheme} {key}" if settings.key_auth_scheme else key
    return headers
