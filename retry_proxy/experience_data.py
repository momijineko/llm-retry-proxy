"""外部分组体验数据（TTFT/倍率）的路径解析与载荷标准化。

这些纯函数从 ``pool_sync`` 拆出，便于独立测试与复用；``PoolSyncManager``
仍负责拉取与缓存，但解析逻辑集中在此模块。
"""

import math
import re
from datetime import datetime, timezone

from .sync_adapters import PoolSyncError

_EXPERIENCE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_EXPERIENCE_TRANSFORM_DEFAULTS = {
    "items_path": "",
    "id_path": "",
    "name_path": "",
    "platform_path": "",
    "rate_path": "",
    "ttft_path": "",
    "ttft_unit": "ms",
    "samples_path": "",
    "timestamp_path": "",
    "detection_path": "",
    "detection_map": {},
    "detection_mode": "field",
    "pass_path": "",
    "fail_path": "",
    "healthy_threshold": 90,
    "warning_threshold": 70,
}


def _ratio_detection(raw, transform):
    counts = [_experience_value(raw, transform[key])
              for key in ("pass_path", "fail_path")]
    if any(type(value) is not int or value < 0 for value in counts):
        raise PoolSyncError("通过次数和失败次数必须为非负整数")
    passed, failed = counts
    total = passed + failed
    if not total:
        status, label = "unknown", "暂无数据"
    elif passed * 100 >= total * transform["healthy_threshold"]:
        status, label = "pass", "通过"
    elif passed * 100 >= total * transform["warning_threshold"]:
        status, label = "degraded", "警告"
    else:
        status, label = "critical", "异常"
    return status, str((transform.get("detection_map") or {}).get(status) or label)


def _experience_timestamp(value):
    if not value:
        return 0.0
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) and numeric > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _experience_value(value, path):
    if path == "$":
        return value
    if not path:
        return None
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _parse_experience_payload(payload, transform=None):
    transform = {**_EXPERIENCE_TRANSFORM_DEFAULTS, **(transform or {})}
    items = _experience_value(payload, transform["items_path"])
    if not isinstance(items, list):
        raise PoolSyncError(f"外部数据列表路径无效: {transform['items_path']}")
    normalized = []
    seen = set()
    for raw in items:
        raw_id = _experience_value(raw, transform["id_path"])
        if not isinstance(raw, dict) or raw_id in (None, ""):
            continue
        item_id = str(raw_id)
        if item_id in seen:
            raise PoolSyncError(f"外部数据包含重复分组 ID: {item_id}")
        seen.add(item_id)
        ttft = _experience_value(raw, transform["ttft_path"])
        try:
            ttft = float(ttft) if ttft is not None else None
            if ttft is not None and transform["ttft_unit"] == "ms":
                ttft /= 1000
            if ttft is not None and (not math.isfinite(ttft) or ttft < 0):
                ttft = None
        except (TypeError, ValueError):
            ttft = None
        sample_count = 1
        if transform["samples_path"]:
            try:
                sample_count = max(int(_experience_value(
                    raw, transform["samples_path"],
                ) or 0), 0)
            except (TypeError, ValueError):
                sample_count = 0
        try:
            rate = float(_experience_value(raw, transform["rate_path"]))
            rate = rate if math.isfinite(rate) else None
        except (TypeError, ValueError):
            rate = None
        item = {
            "id": item_id,
            "name": str(_experience_value(raw, transform["name_path"]) or item_id).strip(),
            "platform": str(_experience_value(
                raw, transform["platform_path"],
            ) or "").strip().lower(),
            "rate_multiplier": rate,
            "ttft": ttft,
            "samples": sample_count,
            "last_ts": _experience_timestamp(_experience_value(
                raw, transform["timestamp_path"],
            )),
        }
        if transform.get("detection_mode") == "pass_ratio":
            item["detection_status"], item["detection_label"] = _ratio_detection(raw, transform)
        elif transform.get("detection_path"):
            status = str(_experience_value(raw, transform["detection_path"]) or "").strip().lower()
            item["detection_status"] = status
            item["detection_label"] = str((transform.get("detection_map") or {}).get(status, "")).strip()
        normalized.append(item)
    return normalized
