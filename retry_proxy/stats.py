from collections import Counter, defaultdict
import os
from datetime import datetime, timedelta


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


def _req_cancelled(r: dict) -> bool:
    """Return whether the downstream ended a Responses stream early."""
    return r.get("stream_status") == "cancelled"


def _req_succeeded(r: dict) -> bool:
    """Prefer the recorded stream outcome, falling back to the HTTP status."""
    if _req_cancelled(r):
        return False
    if "succeeded" in r:
        return bool(r["succeeded"])
    return r.get("final_status", 0) < 400


def _req_first_ok(r: dict) -> bool:
    """Whether the request succeeded on the first attempt (no retries).

    Falls back to ``retries == 0`` for records written by older versions that
    lack an explicit ``first_ok`` field.
    """
    return r.get("first_ok", r.get("retries", 0) == 0)


# 默认别名仅归一化中文显示名；anthropic 等英文 provider 不再硬编码重映射，
# 避免把 Anthropic 流量静默改标。可通过 PROVIDER_ALIASES 环境变量追加/覆盖，
# 格式为 from:to,from:to。
_DEFAULT_PROVIDER_ALIASES = {
    "讯飞星辰 Coding Plan": "xfyun",
}


def _load_extra_aliases():
    raw = os.getenv("PROVIDER_ALIASES", "").strip()
    if not raw:
        return {}
    extras = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        src, dst = entry.split(":", 1)
        src, dst = src.strip(), dst.strip()
        if src and dst:
            extras[src] = dst
    return extras


def reload_aliases():
    """重新加载 PROVIDER_ALIASES 环境变量别名（配置中心热更新时调用）"""
    _PROVIDER_ALIASES.clear()
    _PROVIDER_ALIASES.update(_DEFAULT_PROVIDER_ALIASES)
    _PROVIDER_ALIASES.update(_load_extra_aliases())


_PROVIDER_ALIASES = dict(_DEFAULT_PROVIDER_ALIASES)
reload_aliases()


def _normalize_provider(p: str) -> str:
    return _PROVIDER_ALIASES.get(p, p)


def _model_key(r: dict) -> str:
    p = _normalize_provider(r.get("provider", "") or "(unknown)")
    m = r.get("model", "") or "(unknown)"
    return f"{p}/{m}"


def _agg_by(records: list, key: str, label: str, key_fn=None):
    buckets = defaultdict(lambda: {
        "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0,
        "cancelled": 0, "max_retries": 0, "fail": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0,
        "durations": [], "fail_statuses": Counter(),
    })
    for r in records:
        k = (key_fn(r) if key_fn else (r.get(key, "") or "(unknown)")) or "(unknown)"
        b = buckets[k]
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        b["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        b["completion_tokens"] += r.get("completion_tokens", 0) or 0
        b["total_tokens"] += r.get("total_tokens", 0) or 0
        b["cached_tokens"] += r.get("cached_tokens", 0) or 0
        cancelled = _req_cancelled(r)
        if cancelled:
            b["cancelled"] += 1
        elif _req_succeeded(r):
            b["succeeded"] += 1
            if _req_first_ok(r):
                b["first_ok"] += 1
        else:
            b["fail"] += 1
        # 统计所有上游错误码（含重试过程中的），不限最终失败
        rc = r.get("retry_codes")
        if rc:
            for code in rc:
                b["fail_statuses"][code] += 1
        elif not cancelled and not _req_succeeded(r):
            failure_status = r.get("stream_error_status") or r.get("upstream_status", 0)
            b["fail_statuses"][failure_status] += 1
        b["max_retries"] = max(b["max_retries"], r.get("retries", 0))
        d = r.get("duration_s")
        if isinstance(d, (int, float)):
            b["durations"].append(d)
    out = []
    for k, b in buckets.items():
        ds = sorted(b["durations"])
        n = len(ds)
        measured = b["requests"] - b["cancelled"]
        dom = b["fail_statuses"].most_common(1)
        out.append({
            label: k,
            "requests": b["requests"],
            "retries": b["retries"],
            "avg_retries": round(b["retries"] / b["requests"], 2) if b["requests"] else 0,
            "success_rate": round(b["succeeded"] / measured, 4) if measured else 0,
            "availability_pct": round(b["succeeded"] / measured * 100, 2) if measured else 0,
            "upstream_availability_pct": round(b["first_ok"] / measured * 100, 2) if measured else 0,
            "failed": b["fail"],
            "cancelled": b["cancelled"],
            "max_retries": b["max_retries"],
            "prompt_tokens": b["prompt_tokens"],
            "completion_tokens": b["completion_tokens"],
            "total_tokens": b["total_tokens"],
            "cached_tokens": b["cached_tokens"],
            "avg_duration": round(sum(ds) / n, 3) if n else 0,
            "p95_duration": round(_percentile(ds, 0.95), 3) if n else 0,
            "max_duration": round(ds[-1], 3) if n else 0,
            "dominant_fail_status": dom[0][0] if dom else None,
            "fail_status_count": dom[0][1] if dom else 0,
        })
    out.sort(key=lambda x: x["requests"], reverse=True)
    return out


def _agg_by_key(records: list, key_aliases: dict = None) -> list:
    key_aliases = key_aliases or {}
    attempts = defaultdict(lambda: {
        "attempts": 0, "available": 0, "failed": 0, "ignored": 0, "legacy": 0,
        "events": [], "observations": [],
    })
    requests = {item["key_id"]: item for item in _agg_by(
        records, "key_id", "key_id", key_fn=lambda record: key_aliases.get(record.get("key_id", ""), record.get("key_id", ""))
    ) if item["key_id"] != "(unknown)"}
    for record_index, record in enumerate(records):
        ts = record.get("ts", "") or ""
        trace = record.get("key_attempts")
        if isinstance(trace, list) and trace:
            for attempt_index, item in enumerate(trace):
                if not isinstance(item, dict) or not item.get("key_id"):
                    continue
                key_id = key_aliases.get(item["key_id"], item["key_id"])
                bucket = attempts[key_id]
                bucket["attempts"] += 1
                if item.get("available") is True:
                    bucket["available"] += 1
                    bucket["events"].append((ts, record_index, attempt_index, True))
                elif item.get("available") is False:
                    bucket["failed"] += 1
                    bucket["events"].append((ts, record_index, attempt_index, False))
                else:
                    bucket["ignored"] += 1
                available = item.get("available")
                is_neutral_bridge = (
                    available is None and record.get("stream_status") == "first_event_timeout"
                )
                bucket["observations"].append(
                    (ts, record_index, attempt_index, is_neutral_bridge)
                )
            continue
        raw_key_id = record.get("key_id", "")
        key_id = key_aliases.get(raw_key_id, raw_key_id)
        if key_id:
            bucket = attempts[key_id]
            bucket["attempts"] += 1
            bucket["legacy"] += 1
            if _req_cancelled(record):
                bucket["ignored"] += 1
                continue
            available = _req_succeeded(record)
            bucket["available" if available else "failed"] += 1
            bucket["events"].append((ts, record_index, 0, available))
            bucket["observations"].append((ts, record_index, 0, False))

    out = []
    for key_id in attempts.keys() | requests.keys():
        attempt = attempts[key_id]
        request = requests.get(key_id, {})
        measured = attempt["available"] + attempt["failed"]
        events = sorted(attempt["events"], key=lambda event: (event[0], event[1], event[2]))
        observations = sorted(attempt["observations"], key=lambda event: (event[0], event[1], event[2]))
        latest_available = events[-1][3] if events else None
        latest_neutral = observations[-1][3] if observations else False
        previous_available = events[-2][3] if len(events) > 1 else None
        consecutive_failures = 0
        for event in reversed(events):
            if event[3]:
                break
            consecutive_failures += 1
        success_events = [event for event in events if event[3]]
        failure_events = [event for event in events if not event[3]]
        out.append({
            "key_id": key_id,
            "attempts": attempt["attempts"],
            "failed_attempts": attempt["failed"],
            "ignored_attempts": attempt["ignored"],
            "legacy_attempts": attempt["legacy"],
            "availability_pct": round(attempt["available"] / measured * 100, 2) if measured else None,
            "latest_available": latest_available,
            "latest_neutral": latest_neutral,
            "previous_available": previous_available,
            "consecutive_failures": consecutive_failures,
            "last_attempt_at": events[-1][0] if events else None,
            "last_success_at": success_events[-1][0] if success_events else None,
            "last_failure_at": failure_events[-1][0] if failure_events else None,
            "requests": request.get("requests", 0),
            "request_availability_pct": request.get("availability_pct") if request else None,
            "avg_retries": request.get("avg_retries", 0),
            "max_retries": request.get("max_retries", 0),
            "prompt_tokens": request.get("prompt_tokens", 0),
            "cached_tokens": request.get("cached_tokens", 0),
            "total_tokens": request.get("total_tokens", 0),
        })
    return sorted(out, key=lambda item: item["attempts"], reverse=True)


def _assign_key_pool_records(records: list, pools: list, target: str) -> None:
    """Assign records only when their pool identity is unambiguous."""
    for record in records:
        explicit_pool = record.get("key_pool", "")
        if explicit_pool:
            matches = [pool for pool in pools if pool["id"] == explicit_pool]
        else:
            record_keys = {record.get("key_id", "")}
            for attempt in record.get("key_attempts") or []:
                if isinstance(attempt, dict):
                    record_keys.add(attempt.get("key_id", ""))
            record_keys.discard("")
            provider = _normalize_provider(record.get("provider", ""))
            matches = [pool for pool in pools if _normalize_provider(pool.get("provider", "")) == provider
                       and record_keys & pool["key_set"]]
        if len(matches) == 1:
            matches[0][target].append(record)


def _key_health_status(stats: dict, meta: dict) -> str:
    if meta.get("cooled"):
        return "circuit_open"
    if meta.get("consecutive_failures", 0) > 0:
        return "unavailable"
    if stats.get("latest_neutral"):
        return "available"
    if stats.get("latest_available") is False:
        return "unavailable"
    if stats.get("latest_available") is True:
        return "available"
    return "unknown"


def compute_key_pool_stats(records: list, pool_configs: list, health_records: list = None) -> list:
    pools = []
    for config in pool_configs:
        raw_keys = config.get("keys", ())
        key_ids = tuple(dict.fromkeys(item.get("key_id") if isinstance(item, dict) else item for item in raw_keys))
        key_ids = tuple(key_id for key_id in key_ids if key_id)
        key_meta = {item["key_id"]: item for item in raw_keys if isinstance(item, dict) and item.get("key_id")}
        alias_targets = defaultdict(set)
        for key_id, meta in key_meta.items():
            legacy_key_id = meta.get("legacy_key_id")
            if legacy_key_id and legacy_key_id != key_id:
                alias_targets[legacy_key_id].add(key_id)
        key_aliases = {alias: next(iter(targets)) for alias, targets in alias_targets.items()
                       if len(targets) == 1 and alias not in key_ids}
        pools.append({**config, "keys": key_ids, "key_meta": key_meta,
                      "key_set": set(key_ids) | set(key_aliases), "key_aliases": key_aliases,
                      "records": [], "health_records": []})

    _assign_key_pool_records(records, pools, "records")
    _assign_key_pool_records(health_records if health_records is not None else records, pools, "health_records")

    result = []
    for pool in pools:
        stats_by_key = {item["key_id"]: item for item in _agg_by_key(pool["records"], pool["key_aliases"])}
        health_by_key = {item["key_id"]: item for item in _agg_by_key(pool["health_records"], pool["key_aliases"])}
        key_stats = []
        for key_id in pool["keys"]:
            stats = stats_by_key.get(key_id, {
                "key_id": key_id, "attempts": 0, "failed_attempts": 0, "ignored_attempts": 0,
                "legacy_attempts": 0, "availability_pct": None, "requests": 0,
                "request_availability_pct": None, "avg_retries": 0, "max_retries": 0,
                "prompt_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            })
            health = health_by_key.get(key_id, {})
            for field in ("latest_available", "latest_neutral", "previous_available", "consecutive_failures",
                          "last_attempt_at", "last_success_at", "last_failure_at"):
                stats[field] = health.get(field)
            meta = pool["key_meta"].get(key_id, {})
            key_stats.append({**stats, **meta, "health_status": _key_health_status(stats, meta)})
        result.append({"id": pool["id"], "provider": pool.get("provider", ""),
                       "upstream": pool.get("upstream", pool["id"]), "keys": key_stats})
    return result


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
            if _req_cancelled(r):
                continue
            if t >= cutoff:
                total += 1
                if _req_succeeded(r) and _req_first_ok(r):
                    first_ok += 1
            elif t >= prev_cutoff:
                prev_total += 1
                if _req_succeeded(r) and _req_first_ok(r):
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
        "cancelled": 0, "max_retries": 0, "fail": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0, "durations": [],
    })
    for r in records:
        m = r.get("mode", "") or "off"
        b = buckets[m]
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        b["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        b["completion_tokens"] += r.get("completion_tokens", 0) or 0
        b["total_tokens"] += r.get("total_tokens", 0) or 0
        b["cached_tokens"] += r.get("cached_tokens", 0) or 0
        if _req_cancelled(r):
            b["cancelled"] += 1
        elif _req_succeeded(r):
            b["succeeded"] += 1
            if _req_first_ok(r):
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
        measured = b["requests"] - b["cancelled"]
        out.append({
            "mode": m,
            "mode_label": MODE_LABELS.get(m, "未知/旧数据"),
            "requests": b["requests"],
            "retries": b["retries"],
            "avg_retries": round(b["retries"] / b["requests"], 2) if b["requests"] else 0,
            "succeeded": b["succeeded"],
            "failed": b["fail"],
            "cancelled": b["cancelled"],
            "availability_pct": round(b["succeeded"] / measured * 100, 2) if measured else 0,
            "upstream_availability_pct": round(b["first_ok"] / measured * 100, 2) if measured else 0,
            "prompt_tokens": b["prompt_tokens"],
            "completion_tokens": b["completion_tokens"],
            "total_tokens": b["total_tokens"],
            "cached_tokens": b["cached_tokens"],
            "avg_duration": round(sum(ds) / n, 3) if n else 0,
            "p95_duration": round(_percentile(ds, 0.95), 3) if n else 0,
            "max_retries": b["max_retries"],
        })
    return out


def compute_stats(records: list, range_str: str, config: dict) -> dict:
    total = len(records)
    cancelled = sum(1 for r in records if _req_cancelled(r))
    measured = total - cancelled
    total_retries = sum(r.get("retries", 0) for r in records)
    succeeded = sum(1 for r in records if _req_succeeded(r))
    upstream_ok = sum(1 for r in records if _req_succeeded(r) and _req_first_ok(r))
    failed = measured - succeeded
    avail = round(succeeded / measured * 100, 2) if measured else 0
    upstream_avail = round(upstream_ok / measured * 100, 2) if measured else 0
    total_prompt_tokens = sum(r.get("prompt_tokens", 0) or 0 for r in records)
    total_completion_tokens = sum(r.get("completion_tokens", 0) or 0 for r in records)
    total_tokens = sum(r.get("total_tokens", 0) or 0 for r in records)
    total_cached_tokens = sum(r.get("cached_tokens", 0) or 0 for r in records)

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

    tl = defaultdict(lambda: {
        "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0,
        "failed": 0, "cancelled": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0,
    })
    for r in records:
        ts = r.get("ts", "")
        bucket = ts[:ts_slice] if len(ts) >= ts_slice else ts
        b = tl[bucket]
        b["requests"] += 1
        b["retries"] += r.get("retries", 0)
        b["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        b["completion_tokens"] += r.get("completion_tokens", 0) or 0
        b["total_tokens"] += r.get("total_tokens", 0) or 0
        b["cached_tokens"] += r.get("cached_tokens", 0) or 0
        if _req_cancelled(r):
            b["cancelled"] += 1
        elif _req_succeeded(r):
            b["succeeded"] += 1
            if _req_first_ok(r):
                b["first_ok"] += 1
        else:
            b["failed"] += 1
    timeline = []
    for b, s in sorted(tl.items()):
        bucket_measured = s["requests"] - s["cancelled"]
        timeline.append({
            "ts": b, "requests": s["requests"], "retries": s["retries"],
            "succeeded": s["succeeded"], "failed": s["failed"], "cancelled": s["cancelled"],
            "availability_pct": round(s["succeeded"] / bucket_measured * 100, 2) if bucket_measured else 0,
            "upstream_availability_pct": round(s["first_ok"] / bucket_measured * 100, 2) if bucket_measured else 0,
            "total_tokens": s["total_tokens"],
            "cached_tokens": s["cached_tokens"],
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
    # Streaks must be evaluated in chronological order; records arrive grouped by
    # log file (date) then append order, which is not globally time-sorted across
    # batched writes or multi-day ranges.
    for r in sorted(records, key=lambda r: r.get("ts", "")):
        if _req_cancelled(r):
            continue
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
        "cancelled": _req_cancelled(r),
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

    hour_buckets = defaultdict(lambda: {
        "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "cancelled": 0,
        "total_tokens": 0, "cached_tokens": 0,
    })
    for r in records:
        ts = r.get("ts", "")
        try:
            h = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        hour_buckets[h]["requests"] += 1
        hour_buckets[h]["retries"] += r.get("retries", 0)
        hour_buckets[h]["total_tokens"] += r.get("total_tokens", 0) or 0
        hour_buckets[h]["cached_tokens"] += r.get("cached_tokens", 0) or 0
        if _req_cancelled(r):
            hour_buckets[h]["cancelled"] += 1
        elif _req_succeeded(r):
            hour_buckets[h]["succeeded"] += 1
            if _req_first_ok(r):
                hour_buckets[h]["first_ok"] += 1
    by_hour = []
    for h in range(24):
        s = hour_buckets.get(h, {
            "requests": 0, "retries": 0, "succeeded": 0, "first_ok": 0, "cancelled": 0,
            "total_tokens": 0, "cached_tokens": 0,
        })
        hour_measured = s["requests"] - s["cancelled"]
        by_hour.append({
            "hour": h, "requests": s["requests"], "retries": s["retries"],
            "cancelled": s["cancelled"],
            "availability_pct": round(s["succeeded"] / hour_measured * 100, 2) if hour_measured else 0,
            "upstream_availability_pct": round(s["first_ok"] / hour_measured * 100, 2) if hour_measured else 0,
            "total_tokens": s["total_tokens"],
            "cached_tokens": s["cached_tokens"],
        })

    by_path = _agg_by(records, "path", "path")
    by_path.sort(key=lambda x: x["retries"], reverse=True)
    by_path = by_path[:10]

    return {
        "summary": {
            "total_requests": total,
            "total_retries": total_retries,
            "avg_retries": round(total_retries / total, 2) if total else 0,
            "success_rate": round(succeeded / measured, 4) if measured else 0,
            "availability_pct": avail,
            "upstream_availability_pct": upstream_avail,
            "failed_requests": failed,
            "cancelled_requests": cancelled,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
            "total_cached_tokens": total_cached_tokens,
        },
        "by_provider": _agg_by(records, "provider", "provider"),
        "by_model": [m for m in _agg_by(records, "model", "model", key_fn=_model_key) if not m["model"].endswith("/(unknown)")],
        "by_key": _agg_by_key(records),
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
        "config": config,
    }
