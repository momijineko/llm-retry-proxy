import csv
import fnmatch
import hashlib
import math
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

from collections import OrderedDict

from .config import logger, settings
from .model_capabilities import is_image_model_name

_FAILURE_KIND_PRIORITY = {
    "transport": 1, "cache_miss": 1, "probe": 2, "upstream": 2,
    "rate_limit": 3, "auth": 4,
}
_RUNTIME_FIELDS = (
    "cooldown_until", "total_fail", "last_fail_ts", "consecutive_failures",
    "last_failure_kind", "last_failure_status", "last_cooldown_s",
    "ttft_ewma", "ttft_samples", "ttft_last_ts",
    "probe_latency_s", "probe_last_ts",
)

KEY_POOL_STRATEGIES = {"cost", "ttft", "balanced", "cache"}
_SESSION_ROUTE_LIMIT = 10000
_SESSION_ROUTE_IDLE = 3600.0


def _is_image_model(model):
    return is_image_model_name(model)


class KeyEntry:
    __slots__ = ("key", "key_id", "legacy_key_id", "label", "sort", "group_id", "group_name",
                 "models", "paths", "routing_capabilities", "auth_header", "auth_scheme",
                 "cooldown_until", "total_fail",
                 "last_fail_ts", "consecutive_failures", "last_failure_kind", "last_failure_status",
                 "last_cooldown_s", "ttft_ewma", "ttft_samples", "ttft_last_ts",
                 "probe_latency_s", "probe_last_ts", "_retired")
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
        normalized_capabilities = {}
        if capabilities:
            if "platform" in capabilities:
                normalized_capabilities["platform"] = str(
                    capabilities.get("platform") or ""
                ).strip().lower()
            for field in ("endpoint_families", "model_patterns", "rejected_models",
                          "model_scopes"):
                if field in capabilities:
                    normalized_capabilities[field] = tuple(
                        str(value).strip().lower() for value in capabilities.get(field, ())
                        if str(value).strip()
                    )
            if "rejected_model_routes" in capabilities:
                raw_routes = capabilities.get("rejected_model_routes")
                if isinstance(raw_routes, dict):
                    normalized_capabilities["rejected_model_routes"] = {
                        str(route).strip().lower(): tuple(
                            str(value).strip().lower() for value in models
                            if str(value).strip()
                        ) for route, models in raw_routes.items()
                        if isinstance(models, (list, tuple, set))
                    }
            if "model_list_known" in capabilities:
                normalized_capabilities["model_list_known"] = bool(
                    capabilities.get("model_list_known")
                )
            if "image_generation" in capabilities:
                normalized_capabilities["image_generation"] = bool(
                    capabilities.get("image_generation")
                )
        self.routing_capabilities = normalized_capabilities
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
        # 标记该 entry 是否已被 replace_key_pool 丢弃。调用方跨 await 持有
        # 旧 entry 引用并回写结果时，mark_*/record_ttft 检查此标记后跳过，
        # 避免对已失效的 entry 做无效（且可能误导调度器的）状态更新。
        self._retired = False


class KeyPool:
    """号池选择与调度状态机。

    调度状态字段（ttft/balanced/cache 策略下使用，相互关联）：
      _current            -- 当前粘性 key 的 KeyEntry（None 表示未选号）
      _sticky_until       -- 粘性保持到期时间戳；未到期时优先复用 _current
      _failover_floor     -- 故障转移下限 sort 值；仅 sort >= 此值的 key 可选
      _balanced_group     -- balanced 策略当前选中的分组 key
      _active_probe_group -- 正在执行恢复探针的分组（探针期间独占）
      _probe_cursor_group -- 分组轮转探针的游标（记录上次探针的分组）
      _next_probe_at      -- 下次允许发起探针的时间戳
      _probe_reserved_until -- 探针预留窗口到期时间（探针期间阻止其它探针）

    cost 策略仅使用 _current/_sticky_until/_failover_floor；其余字段在
    ttft/balanced 策略下由 record_ttft/mark_cooldown 驱动状态转移；cache
    策略按请求视图累计输入与缓存 Token，并在每次明确的 usage 后重新排序。
    """
    def __init__(self, keys, provider: str = ""):
        self.entries = [KeyEntry(k[0], k[1] if len(k) > 1 else "") if isinstance(k, tuple) else KeyEntry(k) for k in keys]
        self.provider, self._current, self._sticky_until = provider, None, 0.0
        self.strategy, self.target_ttft_s = "cost", 5.0
        self.target_cache_hit_rate = 0.5
        self.external_retest_weight = 0.5
        self.external_ttft_prior_strength = 2.0
        self.session_affinity = False
        self._session_routes = OrderedDict()
        self._selection_count = 0
        self._views = {}
        self._view_access_sequence = 0
        self._last_view_access = 0
        self._metrics = {}
        self._cache_metrics = OrderedDict()
        self.prior_metrics = {}
        self._balanced_group = None
        self._failover_floor = None
        self._probe_cursor_group = None
        self._next_probe_at = 0.0
        self._probe_reserved_until = 0.0
        self._active_probe_group = None
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

    def views(self):
        """Return the cached per-workload views (read-only access for callers)."""
        return list(self._views.values())

    def apply_settings(self, strategy, target_ttft_s, external_retest_weight,
                       external_ttft_prior_strength, session_affinity,
                       target_cache_hit_rate=None):
        """Apply selection settings and reset scheduler state derived from them.

        Centralizes the update so callers (e.g. PoolSyncManager.set_source_settings)
        do not reach into private scheduler fields directly.
        """
        self.strategy = strategy
        self.target_ttft_s = target_ttft_s
        if target_cache_hit_rate is not None:
            self.target_cache_hit_rate = target_cache_hit_rate
        self.external_retest_weight = external_retest_weight
        self.external_ttft_prior_strength = external_ttft_prior_strength
        self.session_affinity = session_affinity
        self._current = None
        self._sticky_until = 0.0
        self._failover_floor = None
        self._session_routes.clear()
        self._selection_count = 0
        self._balanced_group = None
        self._active_probe_group = None
        self._probe_cursor_group = None
        self._next_probe_at = 0.0
        self._probe_reserved_until = 0.0
        for metric in self._metrics.values():
            metric.update({
                "slow_streak": 0, "recovery_streak": 0,
                "next_probe_at": 0.0, "probe_reserved_until": 0.0,
                "cache_low_streak": 0,
            })

    @staticmethod
    def _capability_matches(entry, model, endpoint_family, model_scope):
        capabilities = entry.routing_capabilities
        if not capabilities:
            return False
        families = capabilities.get("endpoint_families", ())
        if (endpoint_family and "endpoint_families" in capabilities
                and endpoint_family not in families):
            return False
        if model and model in capabilities.get("rejected_models", ()):
            return False
        route_rejections = capabilities.get("rejected_model_routes", {})
        if (model and endpoint_family in route_rejections
                and model in route_rejections[endpoint_family]):
            return False
        if (model and _is_image_model(model) and "image_generation" in capabilities
                and not capabilities.get("image_generation")):
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
            view.target_cache_hit_rate = self.target_cache_hit_rate
            view.external_retest_weight = self.external_retest_weight
            view.external_ttft_prior_strength = self.external_ttft_prior_strength
            view.session_affinity = self.session_affinity
            view.prior_metrics = self.prior_metrics
            view._view_entry_ids = entry_ids
            view._workload = workload
            self._views[signature] = view
        else:
            self._views[signature].strategy = self.strategy
            self._views[signature].target_ttft_s = self.target_ttft_s
            self._views[signature].target_cache_hit_rate = self.target_cache_hit_rate
            self._views[signature].external_retest_weight = self.external_retest_weight
            self._views[signature].external_ttft_prior_strength = self.external_ttft_prior_strength
            self._views[signature].session_affinity = self.session_affinity
            self._views[signature].prior_metrics = self.prior_metrics
        self._view_access_sequence += 1
        self._views[signature]._last_view_access = self._view_access_sequence
        return self._views[signature]

    def _session_route(self, session_id):
        now = time.time()
        route = self._session_routes.get(session_id)
        if route is None:
            if len(self._session_routes) >= _SESSION_ROUTE_LIMIT:
                cutoff = now - _SESSION_ROUTE_IDLE
                expired = [
                    key for key, value in self._session_routes.items()
                    if value.get("last_used", 0.0) < cutoff
                ]
                for key in expired:
                    self._session_routes.pop(key, None)
                if len(self._session_routes) >= _SESSION_ROUTE_LIMIT:
                    self._session_routes.popitem(last=False)
            route = {
                "current": None, "sticky_until": 0.0,
                "failover_floor": None, "last_used": now,
            }
            self._session_routes[session_id] = route
        else:
            self._session_routes.move_to_end(session_id)
        route["last_used"] = now
        return route

    def _pick_for_session(self, session_id, exclude_keys=None):
        route = self._session_route(session_id)
        now = time.time()
        exclude_keys = exclude_keys or set()
        if route["current"] is None and route["failover_floor"] is None:
            return self.pick(exclude_keys=exclude_keys)
        eligible = [
            entry for entry in self.entries
            if entry.key not in exclude_keys
        ]
        if route["failover_floor"] is not None:
            eligible = [
                entry for entry in self.entries
                if entry.key not in exclude_keys
                and self._sort_value(entry) >= route["failover_floor"]
            ]
        available = [entry for entry in eligible if entry.cooldown_until <= now]
        current = route["current"]
        if current is not None and current in available:
            route["sticky_until"] = max(route["sticky_until"], now + settings.key_sticky)
            return current
        if not available:
            return min(eligible, key=lambda entry: entry.cooldown_until) if eligible else None
        return min(available, key=lambda entry: (self._sort_value(entry), self.entries.index(entry)))

    def pick(self, session_id=None, exclude_keys=None):
        exclude_keys = exclude_keys or set()
        if self.session_affinity and session_id:
            return self._pick_for_session(session_id, exclude_keys)
        now = time.time()
        eligible = [
            entry for entry in self._eligible_entries()
            if entry.key not in exclude_keys
        ]
        available = [entry for entry in eligible if entry.cooldown_until <= now]
        if not available:
            best = min(eligible, key=lambda e: e.cooldown_until) if eligible else None
            return best
        if self.strategy == "balanced":
            selected_group = self._pick_group(available)
            available = [entry for entry in available if self._group_key(entry) == selected_group]
            return available[0]
        if (self._current is not None and now < self._sticky_until
                and self._current in available):
            self._sticky_until = now + settings.key_sticky
            return self._current
        if self.strategy == "cost":
            # cost 策略直接按 sort 值选最便宜可用 key，无需按分组筛选
            return available[0]
        selected_group = self._pick_group(available)
        available = [entry for entry in available if self._group_key(entry) == selected_group]
        return available[0]

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

    def _eligible_entries(self):
        if self._failover_floor is None:
            return self.entries
        return [entry for entry in self.entries
                if self._sort_value(entry) >= self._failover_floor]

    def _group_metrics(self, entries):
        groups = {}
        for index, entry in enumerate(entries):
            group = groups.setdefault(self._group_key(entry), {
                "entries": [], "sort": self._sort_value(entry), "index": index,
                "ttft": None, "samples": 0, "last_ts": 0.0,
                "cache_hit_rate": None, "cache_samples": 0,
                "cache_input_tokens": 0, "cache_cached_tokens": 0,
                "cache_last_ts": 0.0, "cache_low_streak": 0,
                "cache_eligible_samples": 0,
            })
            group["entries"].append(entry)
            group["sort"] = min(group["sort"], self._sort_value(entry))
        now = time.time()
        stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
        try:
            prior_strength = max(float(self.external_ttft_prior_strength), 0.0)
        except (TypeError, ValueError):
            prior_strength = 0.0
        for key, group in groups.items():
            prior = self.prior_metrics.get(key) or {}
            external_ttft = prior.get("ttft")
            external_last_ts = float(prior.get("last_ts") or 0.0)
            external_fresh = (
                self.strategy == "ttft" and prior_strength > 0
                and external_ttft is not None
                and (not external_last_ts or now - external_last_ts < stale_after)
            )
            metric = self._metrics.get(key)
            if metric:
                cache_input_tokens = metric.get("cache_input_tokens", 0)
                group["cache_samples"] = metric.get("cache_samples", 0)
                group["cache_input_tokens"] = cache_input_tokens
                group["cache_cached_tokens"] = metric.get("cache_cached_tokens", 0)
                group["cache_last_ts"] = metric.get("cache_last_ts", 0.0)
                group["cache_low_streak"] = metric.get("cache_low_streak", 0)
                group["cache_eligible_samples"] = metric.get("cache_eligible_samples", 0)
                if cache_input_tokens > 0:
                    group["cache_hit_rate"] = min(
                        group["cache_cached_tokens"] / cache_input_tokens, 1.0,
                    )
                group["slow_streak"] = metric["slow_streak"]
                group["recovery_streak"] = metric["recovery_streak"]
                group["next_probe_at"] = metric["next_probe_at"]
                group["probe_reserved_until"] = metric["probe_reserved_until"]
                if metric["samples"]:
                    group["ttft"] = metric["ewma"]
                    group["samples"] = metric["samples"]
                    group["last_ts"] = metric["last_ts"]
                    group["metric_source"] = "local"
                    if external_fresh:
                        local_weight = float(metric["samples"])
                        group["ttft"] = (
                            group["ttft"] * local_weight
                            + float(external_ttft) * prior_strength
                        ) / (local_weight + prior_strength)
                        group["metric_source"] = "blended"
                    continue
            if external_fresh:
                group["ttft"] = float(external_ttft)
                group["samples"] = prior.get("samples", 0)
                group["last_ts"] = external_last_ts
                group["metric_source"] = "external"
            elif self.strategy == "ttft" and external_last_ts:
                group["last_ts"] = external_last_ts
        return groups

    @staticmethod
    def _setting(name, default):
        return getattr(settings, name, default)

    def _metric(self, group_key):
        return self._metrics.setdefault(group_key, {
            "ewma": None, "samples": 0, "last_ts": 0.0,
            "slow_streak": 0, "recovery_streak": 0,
            "next_probe_at": 0.0, "probe_reserved_until": 0.0,
            "cache_samples": 0, "cache_input_tokens": 0,
            "cache_cached_tokens": 0, "cache_last_ts": 0.0,
            "cache_low_streak": 0, "cache_eligible_samples": 0,
        })

    def _ordered_probe_candidates(self, candidates, now):
        try:
            weight = min(max(float(self.external_retest_weight), 0.0), 1.0)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0 or len(candidates) < 2:
            return candidates
        stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
        external = {}
        for key, _ in candidates:
            prior = self.prior_metrics.get(key) or {}
            value = prior.get("ttft")
            last_ts = float(prior.get("last_ts") or 0.0)
            if value is None or last_ts and now - last_ts >= stale_after:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                external[key] = value
        if not external:
            return candidates
        known = sorted(external, key=lambda key: (external[key], key))
        denominator = max(len(known) - 1, 1)
        external_ranks = {
            key: index / denominator for index, key in enumerate(known)
        }
        cost_denominator = max(len(candidates) - 1, 1)
        ranked = []
        for cost_index, candidate in enumerate(candidates):
            key = candidate[0]
            cost_rank = cost_index / cost_denominator
            external_rank = external_ranks.get(key, 1.0)
            score = (1 - weight) * cost_rank + weight * external_rank
            ranked.append((score, external_rank, cost_rank, cost_index, candidate))
        return [item[-1] for item in sorted(ranked, key=lambda item: item[:-1])]

    def _balanced_pick(self, groups):
        now = time.time()
        ordered = sorted(groups.items(), key=lambda pair: (pair[1]["sort"], pair[1]["index"]))
        available_keys = {key for key, _ in ordered}
        current_key = self._group_key(self._current) if self._current is not None else None
        if self._balanced_group not in available_keys:
            self._balanced_group = current_key if current_key in available_keys else ordered[0][0]
        if current_key not in available_keys:
            return self._cache_choice_in_tier(groups, self._balanced_group)

        current = groups[current_key]
        interval = max(float(self._setting("key_ttft_retest_interval", 60)), 0.0)
        reserve_for = max(float(self._setting("key_ttft_retest_interval", 60)), 1.0)
        cheaper = [(key, item) for key, item in ordered
                   if item["sort"] < current["sort"]]
        cheaper = self._ordered_probe_candidates(cheaper, now)
        if self._active_probe_group is not None and now >= self._probe_reserved_until:
            self._active_probe_group = None
        if (cheaper and now >= self._next_probe_at
                and self._active_probe_group is None):
            # Reserve one cheaper group per interval and advance the cursor immediately.
            keys = [key for key, _ in cheaper]
            start = 0
            if self._probe_cursor_group in keys:
                start = (keys.index(self._probe_cursor_group) + 1) % len(keys)
            for offset in range(len(keys)):
                key = keys[(start + offset) % len(keys)]
                metric = self._metric(key)
                if now < metric["next_probe_at"]:
                    continue
                self._probe_cursor_group = key
                self._next_probe_at = now + interval
                self._probe_reserved_until = now + reserve_for
                self._active_probe_group = key
                metric["probe_reserved_until"] = self._probe_reserved_until
                return key

        if (self._balanced_group in groups
                and groups[self._balanced_group]["sort"] > current["sort"]):
            return self._cache_choice_in_tier(groups, self._balanced_group)
        return self._cache_choice_in_tier(groups, current_key)

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
        if self.strategy == "cache":
            return self._cache_group_choice(groups)
        return self._balanced_pick(groups)

    def _cache_group_choice(self, groups):
        current_key = self._group_key(self._current) if self._current is not None else None
        current = groups.get(current_key)
        try:
            confirmations = max(int(self._setting("key_cache_hit_confirmations", 3)), 1)
        except (TypeError, ValueError):
            confirmations = 3
        if (current is not None
                and current.get("cache_eligible_samples", 0) > 0
                and current.get("cache_low_streak", 0) < confirmations):
            return current_key
        candidates = {
            key: item for key, item in groups.items()
            if key != current_key
        } or groups
        acceptable = [
            (key, item) for key, item in candidates.items()
            if item["cache_hit_rate"] is not None
            and item["cache_hit_rate"] >= self.target_cache_hit_rate
        ]
        if acceptable:
            return min(
                acceptable,
                key=lambda pair: (-pair[1]["cache_hit_rate"], pair[1]["sort"],
                                  pair[1]["index"]),
            )[0]
        unknown = [
            (key, item) for key, item in candidates.items()
            if item["cache_hit_rate"] is None
        ]
        if unknown:
            return min(
                unknown,
                key=lambda pair: (pair[1]["cache_last_ts"], pair[1]["sort"],
                                  pair[1]["index"]),
            )[0]
        return min(
            candidates.items(),
            key=lambda pair: (-pair[1]["cache_hit_rate"], pair[1]["sort"],
                              pair[1]["index"]),
        )[0]

    def _cache_choice_in_tier(self, groups, group_key):
        target = groups.get(group_key)
        if target is None:
            return group_key
        tier = {
            key: item for key, item in groups.items()
            if item["sort"] == target["sort"]
        }
        return self._cache_group_choice(tier)

    def record_ttft(self, entry, seconds, alpha=0.3):
        if entry is None or seconds < 0 or getattr(entry, "_retired", False):
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
            is_recovery_probe = group_key == self._active_probe_group
            if is_recovery_probe:
                current_groups = self._group_metrics(self.entries)
                current_key = (self._group_key(self._current)
                               if self._current is not None else None)
                current = current_groups.get(current_key)
                candidate = current_groups.get(group_key)
                is_cheaper = bool(current and candidate and candidate["sort"] < current["sort"])
                if is_cheaper and seconds < lower:
                    metric["recovery_streak"] = 0
                    metric["slow_streak"] = 0
                    self._balanced_group = group_key
                    self._current = entry
                    self._sticky_until = now + settings.key_sticky
                    self._failover_floor = None
                    self._probe_cursor_group = None
                    self._next_probe_at = now + retest_wait
                elif is_cheaper:
                    metric["recovery_streak"] = 0
                    metric["next_probe_at"] = now + stale_wait
                self._active_probe_group = None
                self._probe_reserved_until = 0.0
            elif group_key == self._balanced_group:
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
        if entry is None or seconds < 0 or getattr(entry, "_retired", False):
            return
        group_key = self._group_key(entry)
        now = time.time()
        for candidate in self.entries:
            if self._group_key(candidate) == group_key:
                candidate.probe_latency_s = seconds
                candidate.probe_last_ts = now

    def record_cache_usage(self, entry, input_tokens, cached_tokens, session_id=""):
        """Track Responses cache reads and cool a consistently cold group."""
        if entry is None or getattr(entry, "_retired", False):
            return False
        group_key = self._group_key(entry)
        if input_tokens > 0:
            group_metric = self._metric(group_key)
            group_metric["cache_samples"] += 1
            group_metric["cache_input_tokens"] += input_tokens
            group_metric["cache_cached_tokens"] += cached_tokens
            group_metric["cache_last_ts"] = time.time()
            min_input = max(int(self._setting("key_cache_miss_min_input_tokens", 1024)), 0)
            if input_tokens >= min_input:
                group_metric["cache_eligible_samples"] = (
                    group_metric.get("cache_eligible_samples", 0) + 1
                )
                try:
                    confirmations = max(
                        int(self._setting("key_cache_hit_confirmations", 3)), 1,
                    )
                except (TypeError, ValueError):
                    confirmations = 3
                hit_rate = min(max(cached_tokens / input_tokens, 0.0), 1.0)
                if hit_rate < self.target_cache_hit_rate:
                    group_metric["cache_low_streak"] = min(
                        group_metric.get("cache_low_streak", 0) + 1,
                        confirmations,
                    )
                else:
                    group_metric["cache_low_streak"] = 0
            if self.strategy == "cache":
                self._sticky_until = 0.0
        if not session_id:
            return False
        threshold = max(int(self._setting("key_cache_miss_threshold", 3)), 0)
        cooldown = max(float(self._setting("key_cache_miss_cooldown", 3600)), 0.0)
        min_input = max(int(self._setting("key_cache_miss_min_input_tokens", 1024)), 0)
        if threshold == 0 or cooldown == 0 or input_tokens < min_input:
            return False

        metric_key = (group_key, session_id)
        metric = self._cache_metrics.get(metric_key)
        if metric is None:
            if len(self._cache_metrics) >= _SESSION_ROUTE_LIMIT:
                self._cache_metrics.popitem(last=False)
            metric = {
                "miss_streak": 0, "last_ts": 0.0, "circuit_until": 0.0,
            }
            self._cache_metrics[metric_key] = metric
        else:
            self._cache_metrics.move_to_end(metric_key)
        now = time.time()
        metric["last_ts"] = now
        if cached_tokens > 0:
            for key in list(self._cache_metrics):
                if key[0] == group_key:
                    self._cache_metrics.pop(key, None)
            cleared = False
            for peer in self.entries:
                if (self._group_key(peer) == group_key
                        and peer.last_failure_kind == "cache_miss"):
                    peer.cooldown_until = 0.0
                    peer.consecutive_failures = 0
                    peer.last_failure_kind = ""
                    peer.last_failure_status = None
                    peer.last_cooldown_s = 0.0
                    cleared = True
            if cleared:
                self._failover_floor = None
            return False

        metric["miss_streak"] += 1
        if metric["miss_streak"] < threshold:
            return False
        alternatives = {
            self._group_key(candidate) for candidate in self.entries
            if self._group_key(candidate) != group_key
            and candidate.cooldown_until <= now
        }
        if not alternatives:
            return False

        if metric["circuit_until"] <= now:
            metric["circuit_until"] = now + cooldown
            logger.warning(
                "号池分组连续无缓存，已长时间熔断: "
                f"group={entry.group_name or group_key} workload={self._workload[0]}/"
                f"{self._workload[1]} misses={metric['miss_streak']} cooldown={cooldown:.0f}s"
            )
        remaining = max(metric["circuit_until"] - now, 0.0)
        for peer in self.entries:
            if self._group_key(peer) == group_key:
                self.mark_cooldown(
                    peer, remaining, failure_kind="cache_miss", status=None,
                )
        return True

    def reset_cache_circuit(self, group_id=None):
        targets = [self, *self._views.values()]
        for target in targets:
            if group_id is None:
                target._cache_metrics.clear()
            else:
                group_key = str(group_id)
                for key in list(target._cache_metrics):
                    if key[0] == group_key:
                        target._cache_metrics.pop(key, None)

    def reset_circuit(self, group_id=None, entry_key=None):
        """Clear manual circuit state in the base pool and cached request views."""
        group_key = str(group_id) if group_id is not None else None
        selected = [
            entry for entry in self.entries
            if (group_key is None or self._group_key(entry) == group_key)
            and (entry_key is None or entry.key == entry_key)
        ]
        for entry in selected:
            entry.cooldown_until = 0.0
            entry.consecutive_failures = 0
            entry.last_failure_kind = ""
            entry.last_failure_status = None
            entry.last_cooldown_s = 0.0

        reset_groups = {self._group_key(entry) for entry in selected}
        reset_all = group_id is None and entry_key is None
        targets = [self, *self._views.values()]
        for target in targets:
            if not reset_all and not any(entry in target.entries for entry in selected):
                continue
            target._current = None
            target._sticky_until = 0.0
            target._failover_floor = None
            target._session_routes.clear()
            target._balanced_group = None
            target._probe_cursor_group = None
            if group_key is None or target._active_probe_group in reset_groups:
                target._active_probe_group = None
                target._probe_reserved_until = 0.0
            if group_key is None or reset_groups:
                target._next_probe_at = 0.0
            for key in reset_groups:
                metric = target._metrics.get(key)
                if metric is not None:
                    metric["next_probe_at"] = 0.0
                    metric["probe_reserved_until"] = 0.0
        if reset_all:
            self.reset_cache_circuit()
        else:
            for key in reset_groups:
                self.reset_cache_circuit(key)
        return selected

    def scheduler_status(self, now=None):
        now = time.time() if now is None else now
        stale_after = max(float(self._setting("key_ttft_stale_after", 300)), 0.0)
        confirmations = max(int(self._setting("key_ttft_confirmations", 2)), 1)
        cache_confirmations = max(
            int(self._setting("key_cache_hit_confirmations", 3)), 1,
        )
        result = []
        latest_views = {}
        for view in self._views.values():
            current = latest_views.get(view._workload)
            if current is None or view._last_view_access >= current._last_view_access:
                latest_views[view._workload] = view
        for view in latest_views.values():
            groups = view._group_metrics(view.entries)
            if not groups:
                continue
            current_key = view._balanced_group
            if view.strategy == "cache":
                available_groups = view._group_metrics([
                    entry for entry in view._eligible_entries()
                    if entry.cooldown_until <= now
                ])
                current_key = view._cache_group_choice(available_groups or groups)
            if current_key not in groups and view._current is not None:
                current_key = view._group_key(view._current)
            current = groups.get(current_key)
            local_metric = view._metrics.get(current_key, {}) if current_key else {}
            current_metric = local_metric if local_metric.get("samples") else (
                view.prior_metrics.get(current_key, {})
                if view.strategy == "ttft" and current_key else {}
            )
            metric_source = current.get("metric_source", "") if current else ""
            current_last_ts = current.get("last_ts", 0.0) if current else 0.0
            current_stale = bool(current_last_ts and now - current_last_ts >= stale_after)
            if current is None or current.get("ttft") is None:
                state = "learning"
            elif local_metric.get("slow_streak"):
                state = "slow_confirming"
            elif current_stale:
                state = "stale"
            elif metric_source == "external":
                state = "external"
            elif metric_source == "blended":
                state = "blended"
            else:
                state = "active"
            cheaper = []
            if current is not None:
                candidates = view._ordered_probe_candidates([
                    (key, group) for key, group in groups.items()
                    if group["sort"] < current["sort"]
                ], now)
                for key, group in candidates:
                    metric = view._metrics.get(key, {})
                    prior = view.prior_metrics.get(key) or {}
                    cheaper.append({
                        "group_id": key,
                        "group_name": group["entries"][0].group_name or group["entries"][0].label,
                        "sort": str(group["entries"][0].sort),
                        "recovery_streak": metric.get("recovery_streak", 0),
                        "next_probe_at": metric.get("next_probe_at", 0.0),
                        "probe_inflight": metric.get("probe_reserved_until", 0.0) > now,
                        "external_ttft": prior.get("ttft"),
                    })
            endpoint_family, model = view._workload
            cache_groups = []
            for key, group in groups.items():
                cache_groups.append({
                    "group_id": key,
                    "group_name": (group["entries"][0].group_name
                                   or group["entries"][0].label),
                    "sort": str(group["entries"][0].sort),
                    "hit_rate": round(group["cache_hit_rate"], 6)
                    if group["cache_hit_rate"] is not None else None,
                    "samples": group["cache_samples"],
                    "input_tokens": group["cache_input_tokens"],
                    "cached_tokens": group["cache_cached_tokens"],
                    "last_ts": group["cache_last_ts"],
                    "low_streak": group.get("cache_low_streak", 0),
                })
            result.append({
                "endpoint_family": endpoint_family,
                "model": model,
                "current_group_id": current_key or "",
                "current_group_name": (current["entries"][0].group_name
                                       or current["entries"][0].label) if current else "",
                "current_sort": str(current["entries"][0].sort) if current else "",
                "ttft_ewma": round(current["ttft"], 3)
                if current and current.get("ttft") is not None else None,
                "cache_hit_rate": round(current["cache_hit_rate"], 6)
                if current and current.get("cache_hit_rate") is not None else None,
                "cache_samples": current.get("cache_samples", 0) if current else 0,
                "cache_input_tokens": current.get("cache_input_tokens", 0) if current else 0,
                "cache_cached_tokens": current.get("cache_cached_tokens", 0) if current else 0,
                "cache_low_streak": current.get("cache_low_streak", 0) if current else 0,
                "cache_confirmations": cache_confirmations,
                "cache_groups": sorted(
                    cache_groups,
                    key=lambda item: (
                        item["hit_rate"] is None,
                        -(item["hit_rate"] or 0),
                    ),
                ),
                "samples": current.get("samples", 0) if current else 0,
                "last_ts": current_last_ts,
                "metric_source": metric_source,
                "stale": current_stale,
                "state": state,
                "slow_streak": current_metric.get("slow_streak", 0),
                "confirmations": confirmations,
                "cheaper_groups": cheaper,
            })
        return sorted(result, key=lambda item: (item["endpoint_family"], item["model"]))

    def cache_status(self):
        """Aggregate cache telemetry by group for the key management table."""
        latest_views = {}
        for view in self._views.values():
            current = latest_views.get(view._workload)
            if current is None or view._last_view_access >= current._last_view_access:
                latest_views[view._workload] = view
        result = {}
        for view in latest_views.values():
            for group_key, metric in view._metrics.items():
                input_tokens = metric.get("cache_input_tokens", 0)
                if input_tokens <= 0:
                    continue
                group = result.setdefault(group_key, {
                    "samples": 0, "input_tokens": 0,
                    "cached_tokens": 0, "last_ts": 0.0,
                })
                group["samples"] += metric.get("cache_samples", 0)
                group["input_tokens"] += input_tokens
                group["cached_tokens"] += metric.get("cache_cached_tokens", 0)
                group["last_ts"] = max(
                    group["last_ts"], metric.get("cache_last_ts", 0.0),
                )
        for group in result.values():
            group["hit_rate"] = round(
                min(group["cached_tokens"] / group["input_tokens"], 1.0), 6,
            )
        return result

    def has_fresh(self, exclude_keys=None):
        exclude_keys = exclude_keys or set()
        return any(
            entry.key not in exclude_keys and entry.cooldown_until <= time.time()
            for entry in self._eligible_entries()
        )

    def next_available_in(self):
        eligible = self._eligible_entries()
        if not eligible:
            return 0.0
        return max(min(e.cooldown_until for e in eligible) - time.time(), 0.0)

    def mark_cooldown(self, entry, seconds, ra_wait=None, failure_kind="upstream", backoff=False,
                      max_seconds=None, status=None, session_id=None):
        if entry is None or getattr(entry, "_retired", False):
            return
        now = time.time()
        group_key = self._group_key(entry)
        if group_key == self._active_probe_group:
            self._active_probe_group = None
            self._probe_reserved_until = 0.0
        session_route = self._session_route(session_id) if self.session_affinity and session_id else None
        current = session_route["current"] if session_route is not None else self._current
        if current is not None:
            current_sort = self._sort_value(current)
            if entry is current:
                if session_route is not None:
                    session_route["failover_floor"] = current_sort
                    session_route["sticky_until"] = 0.0
                else:
                    self._failover_floor = current_sort
                    self._sticky_until = 0.0
            elif self._sort_value(entry) < current_sort:
                if session_route is not None:
                    session_route["failover_floor"] = current_sort
                else:
                    self._failover_floor = current_sort
        if session_route is None and self.strategy == "balanced" and group_key != self._balanced_group:
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

    def mark_success(self, entry, session_id=None):
        if entry is None or getattr(entry, "_retired", False):
            return
        entry.cooldown_until = 0.0
        entry.consecutive_failures = 0
        entry.last_failure_kind = ""
        entry.last_failure_status = None
        entry.last_cooldown_s = 0.0
        now = time.time()
        group_key = self._group_key(entry)
        if self.session_affinity and session_id:
            route = self._session_route(session_id)
            current = route["current"]
            if (current is not None and self._sort_value(entry) < self._sort_value(current)
                    and route["failover_floor"] is None):
                return
            route["current"] = entry
            route["sticky_until"] = now + settings.key_sticky
            route["failover_floor"] = None
            return
        if self.strategy == "balanced" and group_key == self._active_probe_group:
            return
        previous_group = self._group_key(self._current) if self._current is not None else None
        self._current = entry
        if self.strategy in ("cache", "balanced") and group_key != previous_group:
            self._metric(group_key)["cache_low_streak"] = 0
        if self.strategy != "balanced" or group_key != previous_group:
            self._sticky_until = now + settings.key_sticky
        if self.strategy == "balanced" and group_key != previous_group:
            self._balanced_group = group_key
            self._next_probe_at = now + max(
                float(self._setting("key_ttft_retest_interval", 60)), 0.0,
            )
            if previous_group is None:
                self._probe_cursor_group = None
        self._failover_floor = None

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
                     key: (
                         {route: list(models) for route, models in value.items()}
                         if key == "rejected_model_routes" and isinstance(value, dict)
                         else list(value) if isinstance(value, tuple) else value
                     )
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
    clone.target_cache_hit_rate = pool.target_cache_hit_rate
    clone.external_retest_weight = pool.external_retest_weight
    clone.external_ttft_prior_strength = pool.external_ttft_prior_strength
    clone.session_affinity = pool.session_affinity
    clone.prior_metrics = {
        key: dict(value) for key, value in pool.prior_metrics.items()
    }
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
    clone._failover_floor = pool._failover_floor
    clone._probe_cursor_group = pool._probe_cursor_group
    clone._next_probe_at = pool._next_probe_at
    clone._probe_reserved_until = pool._probe_reserved_until
    clone._active_probe_group = pool._active_probe_group
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
    retained_keys = set()
    for fresh in replacement.entries:
        entry = old_entries.get(fresh.key)
        if entry is None:
            merged.append(fresh)
            continue
        retained_keys.add(fresh.key)
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

    # Mark entries that were dropped by the swap as retired so in-flight
    # callers holding stale references skip mark_*/record_ttft updates.
    for entry in previous.entries:
        if entry.key not in retained_keys:
            entry._retired = True

    previous.entries[:] = merged
    previous.provider = replacement.provider
    previous.strategy = replacement.strategy
    previous.target_ttft_s = replacement.target_ttft_s
    previous.target_cache_hit_rate = replacement.target_cache_hit_rate
    previous.external_retest_weight = replacement.external_retest_weight
    previous.external_ttft_prior_strength = replacement.external_ttft_prior_strength
    previous.session_affinity = replacement.session_affinity
    previous.prior_metrics = replacement.prior_metrics
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
        view.target_cache_hit_rate = replacement.target_cache_hit_rate
        view.external_retest_weight = replacement.external_retest_weight
        view.external_ttft_prior_strength = replacement.external_ttft_prior_strength
        view.prior_metrics = previous.prior_metrics

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
