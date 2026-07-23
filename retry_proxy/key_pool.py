import csv
import fnmatch
import hashlib
import math
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

from .config import logger, settings

_FAILURE_KIND_PRIORITY = {"transport": 1, "probe": 2, "upstream": 2, "rate_limit": 3, "auth": 4}
_RUNTIME_FIELDS = (
    "cooldown_until", "total_fail", "last_fail_ts", "consecutive_failures",
    "last_failure_kind", "last_failure_status", "last_cooldown_s",
    "ttft_ewma", "ttft_samples", "ttft_last_ts",
    "probe_latency_s", "probe_last_ts",
)

KEY_POOL_STRATEGIES = {"cost", "ttft", "balanced"}


def _is_image_model(model):
    value = (model or "").strip().lower()
    return (
        value.startswith(("gpt-image-", "image", "imagen", "nano-banana"))
        or value.startswith("gemini") and any(
            marker in value for marker in ("image", "imagen", "nano-banana")
        )
    )


class KeyEntry:
    __slots__ = ("key", "key_id", "legacy_key_id", "label", "sort", "group_id", "group_name",
                 "models", "paths", "routing_capabilities", "auth_header", "auth_scheme",
                 "cooldown_until", "total_fail",
                 "last_fail_ts", "consecutive_failures", "last_failure_kind", "last_failure_status",
                 "last_cooldown_s", "ttft_ewma", "ttft_samples", "ttft_last_ts",
                 "probe_latency_s", "probe_last_ts")
    def __init__(self, key: str, label: str = "", models=(), paths=(), sort: str = "",
                 group_id: str = "", group_name: str = "", routing_capabilities=None, auth=None):
        self.key, self.label, self.sort = key, label, sort.strip()
        self.group_id = str(group_id) if group_id not in (None, "") else ""
        self.group_name = group_name
        self.legacy_key_id = label if label else key[:8]
        self.key_id = f"{self.legacy_key_id}|{self.sort}" if self.sort else self.legacy_key_id
        self.models = tuple(pattern.lower() for pattern in models)
        self.paths = tuple(pattern.lstrip("/").lower() for pattern in paths)
        capabilities = routing_capabilities if isinstance(routing_capabilities, dict) else {}
        self.routing_capabilities = {
            "platform": str(capabilities.get("platform") or "").strip().lower(),
            "endpoint_families": tuple(
                str(value).strip().lower() for value in capabilities.get("endpoint_families", ())
                if str(value).strip()
            ),
            "model_patterns": tuple(
                str(value).strip().lower() for value in capabilities.get("model_patterns", ())
                if str(value).strip()
            ),
            "rejected_models": tuple(
                str(value).strip().lower() for value in capabilities.get("rejected_models", ())
                if str(value).strip()
            ),
            "model_scopes": tuple(
                str(value).strip().lower() for value in capabilities.get("model_scopes", ())
                if str(value).strip()
            ),
            "model_list_known": bool(capabilities.get("model_list_known")),
            "image_generation": bool(capabilities.get("image_generation")),
        } if capabilities else {}
        auth = auth if isinstance(auth, dict) else {}
        self.auth_header = str(auth.get("header") or settings.key_auth_header).strip().lower()
        raw_scheme = auth.get("scheme") if "scheme" in auth else None
        self.auth_scheme = settings.key_auth_scheme if raw_scheme is None else str(raw_scheme)
        self.cooldown_until = 0.0
        self.total_fail = 0
        self.last_fail_ts = 0.0
        self.consecutive_failures = 0
        self.last_failure_kind = ""
        self.last_failure_status = None
        self.last_cooldown_s = 0.0
        self.ttft_ewma = None
        self.ttft_samples = 0
        self.ttft_last_ts = 0.0
        self.probe_latency_s = None
        self.probe_last_ts = 0.0


class KeyPool:
    def __init__(self, keys, provider: str = ""):
        self.entries = [KeyEntry(k[0], k[1] if len(k) > 1 else "") if isinstance(k, tuple) else KeyEntry(k) for k in keys]
        self.provider, self._current, self._sticky_until = provider, None, 0.0
        self.strategy, self.target_ttft_s = "cost", 5.0
        self._selection_count = 0
        self._views = {}
        self._metrics = {}
        self._balanced_group = None
        self._view_entry_ids = ()
        self._workload = ("other", "*")
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
        invalid_sorts = set()
        def sort_key(entry):
            if not entry.sort:
                return 1, Decimal(0)
            try:
                value = Decimal(entry.sort)
                if value.is_finite():
                    return 0, value
            except InvalidOperation:
                pass
            invalid_sorts.add(entry.sort)
            return 1, Decimal(0)
        if any(entry.sort for entry in self.entries):
            self.entries.sort(key=sort_key)
        for value in sorted(invalid_sorts):
            logger.warning(f"号池 sort={value!r} 不是有效数字，已保持在有效 sort 之后")
        counts = {}
        for entry in self.entries:
            base = f"{entry.legacy_key_id}|{entry.sort}" if entry.sort else entry.legacy_key_id
            counts[base] = counts.get(base, 0) + 1
        for entry in self.entries:
            base = f"{entry.legacy_key_id}|{entry.sort}" if entry.sort else entry.legacy_key_id
            entry.key_id = base
            if counts[base] > 1:
                fingerprint = hashlib.sha256(entry.key.encode("utf-8")).hexdigest()[:8]
                entry.key_id = f"{base}#{fingerprint}"

    @staticmethod
    def _capability_matches(entry, model, endpoint_family, model_scope):
        capabilities = entry.routing_capabilities
        if not capabilities:
            return False
        families = capabilities.get("endpoint_families", ())
        if endpoint_family and endpoint_family not in families:
            return False
        if model and model in capabilities.get("rejected_models", ()):
            return False
        if model and _is_image_model(model) and not capabilities.get("image_generation"):
            return False
        patterns = capabilities.get("model_patterns", ())
        if capabilities.get("model_list_known"):
            if model and not any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns):
                return False
        elif patterns and (not model or not any(
                fnmatch.fnmatchcase(model, pattern) for pattern in patterns)):
            return False
        scopes = capabilities.get("model_scopes", ())
        if scopes and (not model_scope or model_scope not in scopes):
            return False
        return True

    def has_routing_capabilities(self):
        return any(entry.routing_capabilities for entry in self.entries)

    def for_request(self, model="", path="", endpoint_family="", model_scope=""):
        model = (model or "").lower()
        path = (path or "").lstrip("/").lower()
        endpoint_family = (endpoint_family or "").lower()
        model_scope = (model_scope or "").lower()
        candidates = self.entries
        if endpoint_family and self.has_routing_capabilities():
            candidates = [entry for entry in candidates if self._capability_matches(
                entry, model, endpoint_family, model_scope,
            )]
            if not candidates:
                return None
        matched = [entry for entry in candidates if
                   (model and any(fnmatch.fnmatchcase(model, pattern) for pattern in entry.models)) or
                   (path and any(fnmatch.fnmatchcase(path, pattern) for pattern in entry.paths))]
        selected = matched or [entry for entry in candidates if not entry.models and not entry.paths]
        if not selected:
            return None
        entry_ids = tuple(id(entry) for entry in selected)
        workload = (endpoint_family or "other", model or "*")
        signature = (entry_ids, workload)
        if signature not in self._views:
            view = KeyPool([], self.provider)
            view.entries = selected
            view.strategy = self.strategy
            view.target_ttft_s = self.target_ttft_s
            view._view_entry_ids = entry_ids
            view._workload = workload
            self._views[signature] = view
        else:
            self._views[signature].strategy = self.strategy
            self._views[signature].target_ttft_s = self.target_ttft_s
        return self._views[signature]

    def pick(self):
        now = time.time()
        available = [entry for entry in self.entries if entry.cooldown_until <= now]
        if not available:
            best = min(self.entries, key=lambda e: e.cooldown_until) if self.entries else None
            return best
        if (self._current is not None and now < self._sticky_until
                and self._current in available):
            self._sticky_until = now + settings.key_sticky
            return self._current
        if self.strategy == "cost":
            selected_group = None
        else:
            selected_group = self._pick_group(available)
            available = [entry for entry in available if self._group_key(entry) == selected_group]
        entry = available[0]
        return entry

    @staticmethod
    def _group_key(entry):
        return entry.group_id or entry.key

    @staticmethod
    def _sort_value(entry):
        try:
            value = Decimal(entry.sort)
            return value if value.is_finite() else Decimal("Infinity")
        except InvalidOperation:
            return Decimal("Infinity")

    def _group_metrics(self, entries):
        groups = {}
        for index, entry in enumerate(entries):
            group = groups.setdefault(self._group_key(entry), {
                "entries": [], "sort": self._sort_value(entry), "index": index,
                "ttft": None, "samples": 0, "last_ts": 0.0,
            })
            group["entries"].append(entry)
            group["sort"] = min(group["sort"], self._sort_value(entry))
        for key, group in groups.items():
            metric = self._metrics.get(key)
            if metric:
                group["ttft"] = metric["ewma"]
                group["samples"] = metric["samples"]
                group["last_ts"] = metric["last_ts"]
                group["slow_streak"] = metric["slow_streak"]
                group["recovery_streak"] = metric["recovery_streak"]
                group["next_probe_at"] = metric["next_probe_at"]
                group["probe_reserved_until"] = metric["probe_reserved_until"]
        return groups

    @staticmethod
    def _setting(name, default):
        return getattr(settings, name, default)

    def _metric(self, group_key):
        return self._metrics.setdefault(group_key, {
            "ewma": None, "samples": 0, "last_ts": 0.0,
            "slow_streak": 0, "recovery_streak": 0,
            "next_probe_at": 0.0, "probe_reserved_until": 0.0,
        })

    def _balanced_pick(self, groups):
        now = time.time()
        ordered = sorted(groups.items(), key=lambda pair: (pair[1]["sort"], pair[1]["index"]))
        available_keys = {key for key, _ in ordered}
        if self._balanced_group not in available_keys:
            self._balanced_group = ordered[0][0]
        current = groups[self._balanced_group]
        stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
        reserve_for = max(float(self._setting("key_ttft_retest_interval", 60)), 1.0)
        cheaper = [(key, item) for key, item in ordered
                   if item["sort"] < current["sort"]]
        for key, item in cheaper:
            metric = self._metric(key)
            stale = not metric["last_ts"] or now - metric["last_ts"] >= stale_after
            confirmation_due = bool(metric["recovery_streak"]
                                    and now >= metric["next_probe_at"])
            if ((stale or confirmation_due) and now >= metric["next_probe_at"]
                    and now >= metric["probe_reserved_until"]):
                metric["probe_reserved_until"] = now + reserve_for
                return key
        return self._balanced_group

    def _pick_group(self, entries):
        groups = self._group_metrics(entries)
        self._selection_count += 1
        unknown = [(key, item) for key, item in groups.items() if item["ttft"] is None]
        if self.strategy == "ttft":
            stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
            stale = [(key, item) for key, item in groups.items()
                     if item["last_ts"] and time.time() - item["last_ts"] >= stale_after]
            if unknown or stale:
                candidates = unknown or stale
                return min(candidates, key=lambda pair: (pair[1]["last_ts"], pair[1]["sort"]))[0]
            return min(groups.items(), key=lambda pair: (pair[1]["ttft"], pair[1]["sort"]))[0]
        return self._balanced_pick(groups)

    def record_ttft(self, entry, seconds, alpha=0.3):
        if entry is None or seconds < 0:
            return
        group_key = self._group_key(entry)
        now = time.time()
        metric = self._metric(group_key)
        elapsed = max(now - metric["last_ts"], 0.0) if metric["last_ts"] else 0.0
        stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 1.0)
        dynamic_alpha = max(alpha, 1 - math.exp(-elapsed / stale_after)) if elapsed else 1.0
        metric["ewma"] = (seconds if metric["ewma"] is None
                          else dynamic_alpha * seconds + (1 - dynamic_alpha) * metric["ewma"])
        metric["samples"] += 1
        metric["last_ts"] = now
        metric["probe_reserved_until"] = 0.0

        if self.strategy == "balanced":
            hysteresis = max(float(self._setting("key_ttft_hysteresis", 0.1)), 0.0)
            upper = self.target_ttft_s * (1 + hysteresis)
            lower = self.target_ttft_s * max(1 - hysteresis, 0.0)
            confirmations = max(int(self._setting("key_ttft_confirmations", 2)), 1)
            stale_wait = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
            retest_wait = max(float(self._setting("key_ttft_retest_interval", 60)), 0.0)
            if group_key == self._balanced_group:
                metric["slow_streak"] = metric["slow_streak"] + 1 if seconds > upper else 0
                if metric["slow_streak"] >= confirmations:
                    groups = self._group_metrics([
                        candidate for candidate in self.entries
                        if candidate.cooldown_until <= now
                    ])
                    current = groups.get(group_key)
                    more_expensive = sorted(
                        ((key, item) for key, item in groups.items()
                         if current is not None and item["sort"] > current["sort"]),
                        key=lambda pair: (pair[1]["sort"], pair[1]["index"]),
                    )
                    if more_expensive:
                        metric["slow_streak"] = 0
                        metric["recovery_streak"] = 0
                        metric["next_probe_at"] = now + stale_wait
                        self._balanced_group = more_expensive[0][0]
            else:
                current_groups = self._group_metrics(self.entries)
                current = current_groups.get(self._balanced_group)
                candidate = current_groups.get(group_key)
                is_cheaper = bool(current and candidate and candidate["sort"] < current["sort"])
                if is_cheaper and seconds < lower:
                    metric["recovery_streak"] += 1
                    metric["next_probe_at"] = now + retest_wait
                    if metric["recovery_streak"] >= confirmations:
                        metric["recovery_streak"] = 0
                        metric["slow_streak"] = 0
                        self._balanced_group = group_key
                elif is_cheaper:
                    metric["recovery_streak"] = 0
                    metric["next_probe_at"] = now + stale_wait

        peers = [candidate for candidate in self.entries if self._group_key(candidate) == group_key]
        prior = next((candidate.ttft_ewma for candidate in peers if candidate.ttft_samples), None)
        samples = max((candidate.ttft_samples for candidate in peers), default=0) + 1
        value = seconds if prior is None else alpha * seconds + (1 - alpha) * prior
        for candidate in peers:
            candidate.ttft_ewma = value
            candidate.ttft_samples = samples
            candidate.ttft_last_ts = now

    def record_probe(self, entry, seconds):
        if entry is None or seconds < 0:
            return
        group_key = self._group_key(entry)
        now = time.time()
        for candidate in self.entries:
            if self._group_key(candidate) == group_key:
                candidate.probe_latency_s = seconds
                candidate.probe_last_ts = now

    def scheduler_status(self, now=None):
        now = time.time() if now is None else now
        stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
        confirmations = max(int(self._setting("key_ttft_confirmations", 2)), 1)
        result = []
        for view in self._views.values():
            groups = view._group_metrics(view.entries)
            if not groups:
                continue
            current_key = view._balanced_group
            if current_key not in groups and view._current is not None:
                current_key = view._group_key(view._current)
            current = groups.get(current_key)
            current_metric = view._metrics.get(current_key, {}) if current_key else {}
            current_last_ts = current_metric.get("last_ts", 0.0)
            current_stale = bool(current_last_ts and now - current_last_ts >= stale_after)
            if current is None or not current_metric.get("samples"):
                state = "learning"
            elif current_metric.get("slow_streak"):
                state = "slow_confirming"
            elif current_stale:
                state = "stale"
            else:
                state = "active"
            cheaper = []
            if current is not None:
                for key, group in groups.items():
                    if group["sort"] >= current["sort"]:
                        continue
                    metric = view._metrics.get(key, {})
                    cheaper.append({
                        "group_id": key,
                        "group_name": group["entries"][0].group_name or group["entries"][0].label,
                        "sort": str(group["entries"][0].sort),
                        "recovery_streak": metric.get("recovery_streak", 0),
                        "next_probe_at": metric.get("next_probe_at", 0.0),
                        "probe_inflight": metric.get("probe_reserved_until", 0.0) > now,
                    })
            endpoint_family, model = view._workload
            result.append({
                "endpoint_family": endpoint_family,
                "model": model,
                "current_group_id": current_key or "",
                "current_group_name": (current["entries"][0].group_name
                                       or current["entries"][0].label) if current else "",
                "current_sort": str(current["entries"][0].sort) if current else "",
                "ttft_ewma": (round(current_metric["ewma"], 3)
                              if current_metric.get("ewma") is not None else None),
                "samples": current_metric.get("samples", 0),
                "last_ts": current_last_ts,
                "stale": current_stale,
                "state": state,
                "slow_streak": current_metric.get("slow_streak", 0),
                "confirmations": confirmations,
                "cheaper_groups": cheaper,
            })
        return sorted(result, key=lambda item: (item["endpoint_family"], item["model"]))

    def has_fresh(self):
        return any(e.cooldown_until <= time.time() for e in self.entries)

    def next_available_in(self):
        if not self.entries:
            return 0.0
        return max(min(e.cooldown_until for e in self.entries) - time.time(), 0.0)

    def mark_cooldown(self, entry, seconds, ra_wait=None, failure_kind="upstream", backoff=False,
                      max_seconds=None, status=None):
        now = time.time()
        group_key = self._group_key(entry)
        if self.strategy == "balanced" and group_key != self._balanced_group:
            metric = self._metric(group_key)
            metric["recovery_streak"] = 0
            metric["probe_reserved_until"] = 0.0
            metric["next_probe_at"] = now + max(
                float(self._setting("key_ttft_stale_after", 300)), 0.0,
            )
        already = entry.cooldown_until > now
        if not already:
            entry.consecutive_failures = (entry.consecutive_failures + 1
                                          if entry.last_failure_kind == failure_kind else 1)
            entry.last_failure_kind = failure_kind
            entry.last_failure_status = status
            entry.total_fail += 1
        cooldown = seconds
        if backoff:
            for _ in range(min(max(entry.consecutive_failures - 1, 0), 63)):
                cooldown *= 2
                if max_seconds is not None and cooldown >= max_seconds:
                    break
        if max_seconds is not None:
            cooldown = min(cooldown, max_seconds)
        cooldown = max(cooldown, ra_wait or 0.0)
        proposed_until = now + cooldown
        more_severe = (_FAILURE_KIND_PRIORITY.get(failure_kind, 0)
                       > _FAILURE_KIND_PRIORITY.get(entry.last_failure_kind, 0))
        if already and (more_severe or proposed_until > entry.cooldown_until):
            entry.last_failure_kind = failure_kind
            entry.last_failure_status = status
        entry.cooldown_until = max(entry.cooldown_until, proposed_until)
        entry.last_cooldown_s = max(entry.last_cooldown_s, cooldown) if already else cooldown
        entry.last_fail_ts = now

    def mark_success(self, entry):
        entry.cooldown_until = 0.0
        entry.consecutive_failures = 0
        entry.last_failure_kind = ""
        entry.last_failure_status = None
        entry.last_cooldown_s = 0.0
        self._current = entry
        self._sticky_until = time.time() + settings.key_sticky

    def status(self):
        now = time.time()
        return [{"key_id": e.key_id, "legacy_key_id": e.legacy_key_id, "label": e.label, "sort": e.sort,
                 "cooled": e.cooldown_until > now,
                 "cooldown_remaining": round(max(e.cooldown_until - now, 0), 1), "total_fail": e.total_fail,
                 "consecutive_failures": e.consecutive_failures, "last_failure_kind": e.last_failure_kind,
                 "last_failure_status": e.last_failure_status, "last_cooldown_s": round(e.last_cooldown_s, 1),
                 "group_id": e.group_id, "group_name": e.group_name,
                 "ttft_ewma": round(e.ttft_ewma, 3) if e.ttft_ewma is not None else None,
                 "ttft_samples": e.ttft_samples, "ttft_last_ts": e.ttft_last_ts,
                 "ttft_stale": bool(e.ttft_last_ts and
                                    now - e.ttft_last_ts >= self._setting("key_ttft_stale_after", 300)),
                 "probe_latency_s": round(e.probe_latency_s, 3) if e.probe_latency_s is not None else None,
                 "probe_last_ts": e.probe_last_ts,
                 "models": list(e.models), "paths": list(e.paths),
                 "routing_capabilities": {
                     key: list(value) if isinstance(value, tuple) else value
                     for key, value in e.routing_capabilities.items()
                 }, "auth": {"header": e.auth_header, "scheme": e.auth_scheme}}
                for e in self.entries]


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
        sort = (row.get("sort") or "").strip()
        models = tuple(pattern.strip() for pattern in (row.get("models") or "").split(";") if pattern.strip())
        paths = tuple(pattern.strip() for pattern in (row.get("paths") or "").split(";") if pattern.strip())
        auth = {}
        if (row.get("auth_header") or "").strip():
            auth["header"] = row.get("auth_header")
        auth_scheme = (row.get("auth_scheme") or "").strip()
        if auth_scheme.lower() in ("-", "none"):
            auth["scheme"] = ""
        elif auth_scheme:
            auth["scheme"] = auth_scheme
        if url in pools and provider and pools[url].provider != provider:
            logger.warning(f"号池 key={label or key[:8]} 的 provider={provider!r} 与池现有={pools[url].provider!r} 不一致，已忽略")
        pools.setdefault(url, KeyPool([], provider)).entries.append(KeyEntry(
            key, label, models, paths, sort, auth=auth,
        ))
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


def clone_key_pool(pool: KeyPool) -> KeyPool:
    """Copy pool configuration and health without sharing mutable entries."""
    clone = KeyPool([], pool.provider)
    clone.entries = []
    for entry in pool.entries:
        copied = KeyEntry(
            entry.key, entry.label, entry.models, entry.paths, entry.sort,
            entry.group_id, entry.group_name, entry.routing_capabilities,
            {"header": entry.auth_header, "scheme": entry.auth_scheme},
        )
        for field in _RUNTIME_FIELDS:
            setattr(copied, field, getattr(entry, field))
        clone.entries.append(copied)
    clone.finalize_entries()
    if pool._current is not None:
        clone._current = next(
            (entry for entry in clone.entries if entry.key == pool._current.key), None,
        )
        if clone._current is not None:
            clone._sticky_until = pool._sticky_until
    return clone


def replace_key_pool(url: str, replacement: KeyPool, pools=None):
    """Hot-update one pool while retaining in-flight state for unchanged keys."""
    pools = KEY_POOLS if pools is None else pools
    url = url.rstrip("/")
    previous = pools.get(url)
    if previous is None:
        pools[url] = replacement
        return replacement

    old_entries = {entry.key: entry for entry in previous.entries}
    current_key = previous._current.key if previous._current is not None else None
    merged = []
    for fresh in replacement.entries:
        entry = old_entries.get(fresh.key)
        if entry is None:
            merged.append(fresh)
            continue
        entry.legacy_key_id = fresh.legacy_key_id
        entry.key_id = fresh.key_id
        entry.label = fresh.label
        entry.sort = fresh.sort
        entry.models = fresh.models
        entry.paths = fresh.paths
        entry.group_id = fresh.group_id
        entry.group_name = fresh.group_name
        entry.routing_capabilities = fresh.routing_capabilities
        entry.auth_header = fresh.auth_header
        entry.auth_scheme = fresh.auth_scheme
        merged.append(entry)

    previous.entries[:] = merged
    previous.provider = replacement.provider
    previous.strategy = replacement.strategy
    previous.target_ttft_s = replacement.target_ttft_s
    previous._current = next((entry for entry in merged if entry.key == current_key), None)
    if previous._current is None:
        previous._sticky_until = 0.0

    live_entry_ids = {id(entry) for entry in merged}
    previous._views = {
        signature: view for signature, view in previous._views.items()
        if all(entry_id in live_entry_ids for entry_id in view._view_entry_ids)
    }
    for view in previous._views.values():
        view.provider = replacement.provider
        view.strategy = replacement.strategy
        view.target_ttft_s = replacement.target_ttft_s

    pools[url] = previous
    return previous


def headers_with_key(base_headers: dict, key: Optional[str], auth_header=None, auth_scheme=None) -> dict:
    auth_header = (auth_header or settings.key_auth_header).lower()
    auth_scheme = settings.key_auth_scheme if auth_scheme is None else auth_scheme
    skip_headers = _AUTH_STRIP_HEADERS | {auth_header}
    headers = {k: v for k, v in base_headers.items() if k.lower() not in skip_headers}
    if key:
        headers[auth_header] = f"{auth_scheme} {key}" if auth_scheme else key
    return headers
