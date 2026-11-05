"""AIHub（aihub.top）二次开发版 Sub2API 适配器。

AIHub 的供应商大厅由公开接口 ``/api/v2/public/providers`` 提供分组可售状态、
模型检测结果、按模型的降智健康度以及真实的缓存/成本指标；而
``/api/v1/groups/available`` 只返回站点内全部 active 分组。因此本适配器在
Sub2API 的标准流程之外多做一层判断：

1. **分组筛选**：只有大厅下架、或模型检测明确失败的分组才会被认定为不可用。
   ``available`` 是 AIHub 自己探针的瞬时结果（实测 20~40 秒内 272 个分组中有
   12 个翻转），只能作为调度参考，绝不作为删除 Key 的依据。
2. **按能力屏蔽**：``model_detection.stages`` 区分 text / image / detection 三层，
   text 失败只排除文本端点、image 失败只排除生图，其余能力继续可用。
3. **按模型降智屏蔽**：``model_cards[].health`` 给出分组内单个模型的健康度，
   failed 的模型会写入 ``rejected_models``，只避开该模型而非整组。
4. **有效成本**：``effective_multiplier`` 是把缓存命中折进后的真实倍率
   （实测公式 ``rate × (1-0.9·CH)/(1-0.9·参考CH)``），作为分组排序依据，
   使“最低成本优先”不再被名义倍率误导。

大厅数据量较大（单条目约 22KB、整体近 6MB），因此状态文件中只保存归一化后的
精简快照，不保存原始条目。
"""

import time
import uuid

from ..config import logger, settings
from .base import (PoolSyncError, WriteThrottle, is_rate_limited,
                   request_with_retry, write_delay)
from .sub2api import Sub2APIAdapter, _models_allowed_by_limits, _number_text, _unwrap

# 大厅接口 ``model_detection.status`` 中明确的失败状态。状态缺失或为空时沿用
# 旧行为（保留分组），避免检测尚未完成时误删 Key。
DETECTION_FAILED_STATUSES = frozenset({
    "detection_failed",
    "failed",
    "fail",
    "error",
    "insufficient_evidence",
    "not_configured",
})

# 检测阶段 -> 该阶段失败时应排除的端点族。
STAGE_ENDPOINT_FAMILIES = {
    "text": ("chat", "responses", "messages", "gemini"),
    "image": ("images", "gemini_image"),
    "detection": None,  # 基础可用性阶段，失败即整组不可用
}

# 模型健康度中会被排除该模型的状态。``insufficient`` 只代表样本不足，
# 不作为降智证据，仍保留可用。
MODEL_UNHEALTHY_STATUSES = frozenset({"failed"})

# 有效倍率公式中的缓存读折扣与参考命中率。AIHub 用 0.9（缓存读为输入价的
# 10%）把缓存收益折算进倍率。参考命中率由平台按全局统计滚动更新（实测从
# 0.7469506 漂移到 0.7542126），因此优先使用大厅每轮返回的
# ``range_cache.reference_cache_hit_rate``；下面的常量仅在缺少该字段时兜底。
DEFAULT_CACHE_DISCOUNT = 0.9
DEFAULT_REFERENCE_CACHE_HIT_RATE = 0.7542126

PROVIDERS_PATH = "/api/v2/public/providers"
# 旧版大厅接口会返回 410，仅作为路径兼容的兜底。
PROVIDERS_LEGACY_PATH = "/api/v1/public/providers"


def _bool_value(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "available", "enabled", "active"):
        return True
    if text in ("0", "false", "no", "off", "unavailable", "disabled", "inactive"):
        return False
    return None


def _ratio(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 1 else None


def _multiplier(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def effective_multiplier(rate, cache_hit_rate,
                         reference=DEFAULT_REFERENCE_CACHE_HIT_RATE,
                         discount=DEFAULT_CACHE_DISCOUNT):
    """把缓存命中折算进名义倍率，得到真实成本排序依据。

    与 AIHub 大厅的 ``effective_multiplier`` 等价：
    ``rate × (1 - discount·CH) / (1 - discount·参考CH)``。命中率高于参考值
    时打折，低于参考值时加价。
    """
    rate = _multiplier(rate)
    if rate is None:
        return None
    denominator = 1 - discount * reference
    if denominator <= 0:
        return rate
    cache_hit_rate = _ratio(cache_hit_rate)
    if cache_hit_rate is None:
        # 没有实测命中率时按参考值处理，等价于不调整。
        cache_hit_rate = reference
    return rate * (1 - discount * cache_hit_rate) / denominator


def hall_entry(item):
    """把大厅条目归一化为本适配器使用的精简结构。

    只保留筛选、排序与展示需要的字段；原始条目接近 22KB，不进入状态文件。
    """
    if not isinstance(item, dict):
        return {}
    detection = item.get("model_detection")
    detection = detection if isinstance(detection, dict) else {}
    stages = detection.get("stages")
    stages = stages if isinstance(stages, dict) else {}
    stage_states = {}
    for name in STAGE_ENDPOINT_FAMILIES:
        stage = stages.get(name)
        if isinstance(stage, dict):
            stage_states[name] = str(
                stage.get("published_state") or stage.get("state") or ""
            ).strip().lower()

    unhealthy_models = []
    model_health = {}
    for card in item.get("model_cards") or []:
        if not isinstance(card, dict):
            continue
        model = str(card.get("model") or "").strip().lower()
        health = str(card.get("health") or "").strip().lower()
        if not model or not health:
            continue
        model_health[model] = health
        if health in MODEL_UNHEALTHY_STATUSES:
            unhealthy_models.append(model)

    range_cache = item.get("range_cache")
    range_cache = range_cache if isinstance(range_cache, dict) else {}
    cache_hit_rate = _ratio(range_cache.get("weighted_cache_hit_rate"))
    if cache_hit_rate is None:
        # 部分条目把命中率放在字符串字段里，例如 "56.40%"。
        raw = str(item.get("cache_hit_rate") or "").strip().rstrip("%")
        try:
            cache_hit_rate = _ratio(float(raw) / 100) if raw else None
        except (TypeError, ValueError):
            cache_hit_rate = None
    rate = _multiplier(item.get("rate_multiplier"))
    published_effective = _multiplier(item.get("effective_multiplier"))
    reference = _ratio(range_cache.get("reference_cache_hit_rate"))
    return {
        "group_id": item.get("group_id", item.get("id")),
        "code": str(item.get("code") or item.get("name") or ""),
        "platform": str(item.get("platform") or "").strip().lower(),
        "available": _bool_value(item.get("available")),
        "visible_in_hall": _bool_value(item.get("visible_in_hall")),
        "detection_status": str(detection.get("status") or "").strip().lower(),
        "detection_stages": stage_states,
        "unhealthy_models": sorted(set(unhealthy_models)),
        "model_health": model_health,
        "cache_hit_rate": cache_hit_rate,
        "cache_reference": reference,
        "cache_ready": bool(range_cache.get("ready")),
        "rate_multiplier": rate,
        "effective_multiplier": (
            published_effective if published_effective is not None
            else effective_multiplier(rate, cache_hit_rate, reference or
                                      DEFAULT_REFERENCE_CACHE_HIT_RATE)
        ),
        "success_rate": _ratio((item.get("success_rates") or {}).get("1h")),
        "sla": _ratio(item.get("sla")),
        "ttft_ms": _multiplier(
            (item.get("avg_ttft_ms"))
            or ((item.get("range_cache") or {}).get("avg_ttft_ms"))
        ),
        "long_context_pricing": bool(item.get("long_context_pricing_enabled")),
        "input_multiplier": _multiplier((item.get("input_comparison") or {}).get("multiplier")),
    }


def unavailable_reason(entry):
    """返回分组整体不可用的原因；可用或状态未知时返回空字符串。

    只使用**稳定**字段：大厅下架与模型检测失败。``available`` 是探针瞬时
    状态，不参与判定，否则会把整个号池反复删空。
    """
    if entry.get("visible_in_hall") is False:
        return "大厅已下架"
    status = str(entry.get("detection_status") or "")
    if status in DETECTION_FAILED_STATUSES:
        # detection 阶段（基础可用性）失败才算整组不可用；只有 text/image
        # 失败时改由阶段屏蔽处理，避免误伤其它能力。
        stages = entry.get("detection_stages") or {}
        if stages.get("detection") == "completed":
            return ""
        return f"模型检测未通过({status})"
    return ""


def stage_disabled_families(entry):
    """返回因检测阶段失败而应屏蔽的端点族集合。"""
    disabled = set()
    stages = entry.get("detection_stages") or {}
    for stage, families in STAGE_ENDPOINT_FAMILIES.items():
        if not families:
            continue
        if stages.get(stage) in ("failed", "error"):
            disabled.update(families)
    return disabled


def _hall_items(payload):
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items")
        if items is None:
            nested = payload.get("data")
            items = nested.get("items") if isinstance(nested, dict) else nested
    else:
        items = None
    if not isinstance(items, list):
        raise PoolSyncError("AIHub 供应商大厅响应格式无法识别")
    return items


class AIHubAdapter(Sub2APIAdapter):
    name = "aihub"
    label = "AIHub"
    credential_fields = [
        {"name": "email", "label": "登录邮箱", "type": "email"},
        {"name": "password", "label": "登录密码", "type": "password"},
    ]
    capabilities = ["group_catalog", "create_keys", "delete_keys", "hall_availability"]

    async def _fetch_hall(self, client, source):
        base_url = source["base_url"].rstrip("/")
        last_error = None
        for path in (PROVIDERS_PATH, PROVIDERS_LEGACY_PATH):
            try:
                response = await request_with_retry(
                    client, "GET", base_url + path,
                    headers={"Accept": "application/json"}, timeout=30,
                )
            except Exception as exc:
                last_error = exc
                continue
            if response.status_code >= 400:
                last_error = PoolSyncError(
                    f"供应商大厅接口请求失败 (HTTP {response.status_code})"
                )
                continue
            try:
                return _hall_items(_unwrap(response))
            except PoolSyncError as exc:
                last_error = exc
        raise PoolSyncError(f"读取供应商大厅失败: {last_error}")

    async def hall_state(self, client, source):
        """读取大厅并按分组 ID 归一化；读取失败时回退到上次成功快照。"""
        # 旧版本曾把原始条目写入状态文件（单轮约 6MB），这里顺带清除遗留字段。
        source.pop("hall_items", None)
        try:
            items = await self._fetch_hall(client, source)
        except Exception as exc:
            cached = source.get("hall_groups")
            if not isinstance(cached, dict) or not cached:
                raise
            logger.warning(
                f"AIHub 供应商大厅读取失败，继续使用上次快照: "
                f"upstream={source['base_url']} error={exc}"
            )
            return cached
        groups = {}
        for item in items:
            entry = hall_entry(item)
            group_id = entry.get("group_id")
            if group_id in (None, ""):
                continue
            entry["blocked_reason"] = unavailable_reason(entry)
            groups[str(group_id)] = entry
        source["hall_groups"] = groups
        source["hall_synced_at"] = time.time()
        return groups

    # ---------------------------------------------------------------- 同步

    def _filter_state(self, source, hall):
        """用大厅快照计算本次同步的屏蔽集合。"""
        blocked = {}
        for group_id, entry in hall.items():
            reason = unavailable_reason(entry)
            if reason:
                blocked[group_id] = reason
        # 只有本轮与上一轮**连续**判定不可用时才允许删除 Key，避免大厅抖动
        # 造成反复建删，也避免瞬时数据把号池删空。
        previous = source.get("hall_blocked_pending")
        previous = previous if isinstance(previous, dict) else {}
        confirmed = {group_id for group_id in blocked if group_id in previous}
        source["hall_blocked_pending"] = blocked
        return blocked, confirmed

    async def fetch(self, client, source, session):
        session, keys = await self._fetch_all_keys(client, source, session)
        session, groups = await self._get(client, source, session, "/api/v1/groups/available")
        session, rates = await self._get(client, source, session, "/api/v1/groups/rates")
        hall = await self.hall_state(client, source)
        groups_by_id = {
            str(group.get("id")): group for group in (groups or []) if isinstance(group, dict)
        }
        active_group_ids = {
            key for key, group in groups_by_id.items()
            if group.get("status") in (None, "", "active")
        }
        blocked, confirmed = self._filter_state(source, hall)
        stale_keys = [
            item for item in keys
            if isinstance(item, dict) and item.get("id") is not None
            and item.get("group_id") is not None
            and (str(item.get("group_id")) not in active_group_ids
                 or str(item.get("group_id")) in confirmed)
        ]
        session, deleted = await self._delete_keys(client, source, session, stale_keys)
        if deleted:
            logger.info(
                f"AIHub 失效/不可用分组 Key 已自动删除: upstream={source['base_url']} "
                f"count={deleted}"
            )
        stale_ids = {str(item["id"]) for item in stale_keys}
        keys = [item for item in keys if str(item.get("id")) not in stale_ids]
        rates = {str(key): value for key, value in (rates or {}).items()}
        entries = []
        for item in keys:
            if not isinstance(item, dict) or item.get("status") != "active" or not item.get("key"):
                continue
            group_id = item.get("group_id")
            group = dict(groups_by_id.get(str(group_id)) or {})
            group.update(item.get("group") or {})
            if group_id is None or not group or group.get("status") not in (None, "", "active"):
                continue
            key_name = str(item.get("name") or "").strip()
            group_name = str(group.get("name") or "").strip()
            if key_name and group_name and group_name.lower() not in key_name.lower():
                label = f"{key_name}-{group_name}"
            else:
                label = key_name or group_name
            rate = rates.get(str(group_id), group.get("rate_multiplier"))
            capabilities = self.routing_capabilities(group)
            hall_entry_value = hall.get(str(group_id)) or {}
            capabilities = self._apply_hall_capabilities(capabilities, hall_entry_value)
            entries.append({
                "source_key_id": item.get("id"), "key": item["key"], "label": label,
                "sort": _number_text(rate), "key_name": key_name, "group_id": group_id,
                "group_name": group_name, "platform": group.get("platform", ""),
                "allow_image_generation": bool(group.get("allow_image_generation")),
                "routing_capabilities": capabilities,
                "effective_sort": _number_text(
                    hall_entry_value.get("effective_multiplier")
                ),
            })
        await self._apply_group_model_cache(client, source, entries)
        return session, entries

    @staticmethod
    def _apply_hall_capabilities(capabilities, entry):
        """把大厅的降智 / 阶段失败信息合并进自动能力。"""
        capabilities = dict(capabilities or {})
        if not entry:
            return capabilities
        unhealthy = [
            model for model in entry.get("unhealthy_models") or []
            if str(model).strip()
        ]
        if unhealthy:
            existing = list(capabilities.get("rejected_models") or ())
            merged = sorted({*existing, *[str(model).lower() for model in unhealthy]})
            capabilities["rejected_models"] = merged
        disabled = stage_disabled_families(entry)
        if disabled:
            families = capabilities.get("endpoint_families")
            if families:
                remaining = [family for family in families if family not in disabled]
                if remaining:
                    capabilities["endpoint_families"] = remaining
                    if "images" in disabled or "gemini_image" in disabled:
                        capabilities["image_generation"] = False
                # 全部端点都被屏蔽时保留原能力，交由 unavailable_reason 处理。
        return capabilities

    async def _delete_keys(self, client, source, session, keys, options=None):
        """删除 Key，全程限流。

        同步阶段的自动删除同样走限流：一次同步可能命中上百个待删 Key，
        不限速会直接把上游写接口打满而被阻塞。
        """
        throttle = WriteThrottle(write_delay(
            options, settings.key_pool_delete_delay,
        ))
        deleted = 0
        for index, item in enumerate(keys):
            await throttle.before()
            try:
                session, _ = await self._authorized_delete(
                    client, source, session, f"/api/v1/keys/{item['id']}"
                )
                deleted += 1
                throttle.on_success()
            except Exception as exc:
                logger.warning(
                    f"AIHub 不可用分组 Key 删除失败: upstream={source['base_url']} "
                    f"source_key_id={item['id']} error={exc}"
                )
                if is_rate_limited(exc):
                    throttle.on_rate_limited()
                    logger.warning(
                        f"AIHub 删除 Key 命中限流，本轮停止，剩余 "
                        f"{len(keys) - index - 1} 个留待下次同步: "
                        f"upstream={source['base_url']}"
                    )
                    break
        return session, deleted

    async def create_keys(self, client, source, session, group_ids, only_missing=False,
                          options=None):
        """建 Key，遇到速率限制立即停止而不是继续重试。

        AIHub 对建 Key 接口有较严的频率限制。Sub2API 的基类实现会逐个重试，
        这既会放大限流窗口，也容易在建完后被下一轮同步删掉。这里对 429 做
        短路：记录错误并结束本轮，剩余分组留到下次手动补齐。
        """
        session, groups = await self.catalog(client, source, session)
        groups_by_id = {str(group["id"]): group for group in groups}
        selected = {str(group_id) for group_id in (group_ids or [])}
        if only_missing:
            targets = [group for group in groups if group["key_count"] == 0
                       and (not selected or str(group["id"]) in selected)]
        else:
            targets = [groups_by_id[group_id] for group_id in selected if group_id in groups_by_id]
        if not targets:
            raise PoolSyncError("没有需要创建 Key 的分组")
        prefix = str((options or {}).get("name_prefix") or "").strip()[:40]
        throttle = WriteThrottle(write_delay(
            options, settings.key_pool_create_delay,
        ))
        progress = (options or {}).get("_progress")
        if progress:
            await progress(0, len(targets), 0, 0)
        created = []
        errors = []
        for index, group in enumerate(targets):
            await throttle.before()
            name = f"{prefix}-{group['name']}"[:100] if prefix else str(group["name"])[:100]
            idempotency_key = f"pool-sync-key-{source['id']}-{group['id']}-{uuid.uuid4()}"
            try:
                session, item = await self._authorized_post(
                    client, source, session, "/api/v1/keys",
                    {"name": name, "group_id": group["id"]},
                    {"Idempotency-Key": idempotency_key},
                )
                created.append({
                    "group_id": group["id"], "group_name": group["name"],
                    "key_id": (item or {}).get("id"), "name": name,
                })
                throttle.on_success()
            except Exception as exc:
                errors.append({
                    "group_id": group["id"], "group_name": group["name"], "error": str(exc),
                })
                if is_rate_limited(exc):
                    throttle.on_rate_limited()
                    errors.append({
                        "group_id": None, "group_name": None,
                        "error": (
                            f"上游限流，已停止创建；剩余 "
                            f"{len(targets) - index - 1} 个分组请稍后重试"
                        ),
                    })
                    if progress:
                        await progress(index + 1, len(targets), len(created), len(errors))
                    break
            if progress:
                await progress(index + 1, len(targets), len(created), len(errors))
        return session, {"created": created, "errors": errors, "requested": len(targets),
                         "throttled": throttle.rate_limited > 0}


    # ---------------------------------------------------------------- 分组目录

    async def catalog(self, client, source, session):
        session, keys = await self._fetch_all_keys(client, source, session)
        session, groups = await self._get(client, source, session, "/api/v1/groups/available")
        session, rates = await self._get(client, source, session, "/api/v1/groups/rates")
        hall = await self.hall_state(client, source)
        rates = {str(key): value for key, value in (rates or {}).items()}
        counts = {}
        active_counts = {}
        for item in keys:
            group_id = item.get("group_id") if isinstance(item, dict) else None
            if group_id is None:
                continue
            key = str(group_id)
            counts[key] = counts.get(key, 0) + 1
            if item.get("status") == "active":
                active_counts[key] = active_counts.get(key, 0) + 1
        catalog = []
        model_cache = source.get("group_model_cache")
        model_cache = model_cache if isinstance(model_cache, dict) else {}
        for group in groups or []:
            if not isinstance(group, dict) or group.get("status") not in (None, "", "active"):
                continue
            group_id = group.get("id")
            key = str(group_id)
            hall_item = hall.get(key) or {}
            if unavailable_reason(hall_item):
                # 大厅下架或检测失败的分组不进入目录，也就不会成为建 Key 的目标。
                continue
            capabilities = self.routing_capabilities(group)
            capabilities = self._apply_hall_capabilities(capabilities, hall_item)
            if key in model_cache and capabilities and isinstance(model_cache[key], list):
                models = list(model_cache[key])
                configured = capabilities.get("model_patterns") or []
                if capabilities.get("model_list_known"):
                    models = _models_allowed_by_limits(models, configured)
                capabilities["model_patterns"] = models
                capabilities["model_list_known"] = True
            effective = hall_item.get("effective_multiplier")
            catalog.append({
                "id": group_id, "name": group.get("name", ""),
                "platform": group.get("platform", ""),
                "allow_image_generation": bool(group.get("allow_image_generation")),
                "routing_capabilities": capabilities,
                "rate_multiplier": _number_text(rates.get(key, group.get("rate_multiplier"))),
                "effective_multiplier": _number_text(effective),
                "key_count": counts.get(key, 0), "active_key_count": active_counts.get(key, 0),
                "hall_available": hall_item.get("available"),
                "hall_visible": hall_item.get("visible_in_hall"),
                "hall_detection_status": hall_item.get("detection_status", ""),
                "hall_cache_hit_rate": hall_item.get("cache_hit_rate"),
                "hall_success_rate": hall_item.get("success_rate"),
                "hall_unhealthy_models": list(hall_item.get("unhealthy_models") or ()),
                "hall_ttft_ms": hall_item.get("ttft_ms"),
            })
        return session, catalog
