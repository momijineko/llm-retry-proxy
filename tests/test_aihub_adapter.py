import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from retry_proxy.sync_adapters import ADAPTERS, PoolSyncError
from retry_proxy.sync_adapters.aihub import (AUTO_CREATE_MAX_PER_ROUND,
                                             AIHubAdapter,
                                             DEFAULT_REFERENCE_CACHE_HIT_RATE,
                                             effective_multiplier, hall_entry,
                                             participation_reason,
                                             stage_disabled_families)

PARTICIPATION_EXCLUDED_STATUSES = (
    "detection_failed", "failed", "fail", "error", "insufficient_evidence",
    "not_configured", "suspected", "not_tested", "unknown",
)


def api_response(data=None, status=200):
    return httpx.Response(
        status, json={"code": 0, "message": "success", "data": data},
        request=httpx.Request("GET", "https://aihub.test"),
    )


def hall_response(items):
    return httpx.Response(
        200, json={"code": 0, "message": "success", "data": {
            "generated_at": "2026-11-05T07:10:19Z", "total": len(items),
            "items": items,
        }},
        request=httpx.Request("GET", "https://aihub.test"),
    )


def hall_item(group_id, *, available=True, status="passed", visible=True, code=None,
              stages=None, cards=None, cache_hit=None, rate=0.1, effective=None,
              success_rate=0.98, excluded_models=None):
    item = {
        "group_id": group_id,
        "code": code or f"G{group_id}",
        "platform": "openai",
        "rate_multiplier": rate,
        "available": available,
        "visible_in_hall": visible,
        "success_rates": {"1h": success_rate},
        "sla": success_rate,
        "long_context_pricing_enabled": False,
        "model_detection": {"status": status, "applicable": True,
                            "stages": stages or {}},
        "model_cards": [
            {"model": model, "health": health}
            for model, health in (cards or [])
        ],
    }
    if cache_hit is not None:
        item["range_cache"] = {
            "ready": True, "weighted_cache_hit_rate": cache_hit,
            "reference_cache_hit_rate": DEFAULT_REFERENCE_CACHE_HIT_RATE,
        }
        item["cache_hit_rate"] = f"{cache_hit * 100:.2f}%"
    if effective is not None:
        item["effective_multiplier"] = effective
    if excluded_models:
        item["_excluded"] = excluded_models
    return item


def group(group_id, name=None, status="active"):
    return {
        "id": group_id, "name": name or f"G{group_id}", "platform": "openai",
        "status": status, "rate_multiplier": 0.1, "allow_image_generation": False,
    }


def key(key_id, group_id, key_value=None, status="active"):
    return {
        "id": key_id, "key": key_value or f"sk-key-{key_id}", "name": f"key{key_id}",
        "group_id": group_id, "status": status,
    }


class FakeAIHubClient:
    def __init__(self, groups, keys, hall, rates=None):
        self.groups = groups
        self.keys = keys
        self.hall = hall
        self.rates = rates or {}
        self.deleted = []
        self.created = []
        self.post_errors = {}

    async def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/api/v2/public/providers"):
            return hall_response(self.hall)
        if url.endswith("/api/v1/groups/available"):
            return api_response(self.groups)
        if url.endswith("/api/v1/groups/rates"):
            return api_response(self.rates)
        if url.endswith("/api/v1/keys"):
            return api_response({"items": list(self.keys), "total": len(self.keys)})
        if url.endswith("/v1/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": "gpt-5.4"}]},
                request=httpx.Request("GET", url),
            )
        raise AssertionError(url)

    async def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return api_response({})

    async def post(self, url, json=None, headers=None, timeout=None):
        if not url.endswith("/api/v1/keys"):
            raise AssertionError(url)
        group_id = json["group_id"]
        if group_id in self.post_errors:
            raise RuntimeError(self.post_errors[group_id])
        self.created.append(group_id)
        created_key = key(9000 + group_id, group_id, f"sk-created-{group_id}")
        created_key["name"] = json.get("name") or created_key["name"]
        self.keys.append(created_key)
        return api_response({"id": created_key["id"], "group_id": group_id})


def source():
    return {
        "id": "source-aihub", "adapter": "aihub",
        "base_url": "https://aihub.test", "provider": "aihub",
    }


def session():
    return {"email": "user@example.com", "access_token": "access-1",
            "refresh_token": "refresh-1"}


class AIHubAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_is_registered(self):
        self.assertIsInstance(ADAPTERS["aihub"], AIHubAdapter)
        self.assertEqual(ADAPTERS["aihub"].label, "AIHub")

    # ------------------------------------------------------------ 有效倍率

    def test_effective_multiplier_matches_published_formula(self):
        # 用当前大厅参考命中率复现官方计算公式
        reference = DEFAULT_REFERENCE_CACHE_HIT_RATE
        self.assertAlmostEqual(
            effective_multiplier(0.045, 0.564),
            0.045 * (1 - 0.9 * 0.564) / (1 - 0.9 * reference), places=9,
        )
        # CH 等于参考命中率时不调整
        self.assertAlmostEqual(
            effective_multiplier(0.05, reference), 0.05, places=9,
        )
        # CH 高于参考值打折，低于参考值加价
        self.assertLess(effective_multiplier(0.05, 0.95), 0.05)
        self.assertGreater(effective_multiplier(0.05, 0.1), 0.05)
        # 缺少命中率时视为参考值
        self.assertAlmostEqual(effective_multiplier(0.05, None), 0.05, places=9)
        self.assertIsNone(effective_multiplier(None, 0.5))

    def test_effective_multiplier_prefers_published_value(self):
        entry = hall_entry(hall_item(1, rate=0.05, cache_hit=0.9, effective=0.0123))
        self.assertAlmostEqual(entry["effective_multiplier"], 0.0123, places=6)
        inferred = hall_entry(hall_item(1, rate=0.05, cache_hit=0.9))
        self.assertLess(inferred["effective_multiplier"], 0.05)

    def test_effective_multiplier_uses_hall_reference_when_available(self):
        low_ref = hall_entry(hall_item(1, rate=0.05, cache_hit=0.5))
        low_ref["cache_reference"] = 0.5
        # 参考命中率等于当前命中率时，有效倍率应等于名义倍率
        self.assertAlmostEqual(
            effective_multiplier(0.05, 0.5, reference=0.5), 0.05, places=9,
        )
        self.assertIsNotNone(low_ref["effective_multiplier"])

    # ------------------------------------------------------------ 筛选语义

    def test_available_is_not_a_participation_signal(self):
        # available 是探针瞬时状态，不能作为参与判定依据
        entry = hall_entry(hall_item(1, available=False))
        self.assertFalse(entry["available"])
        self.assertEqual(participation_reason(entry), "")
        self.assertEqual(
            participation_reason(hall_entry(hall_item(1, available=True))), "",
        )

    def test_participation_requires_passed_detection(self):
        self.assertEqual(participation_reason(hall_entry(hall_item(1))), "")
        self.assertEqual(
            participation_reason(hall_entry(hall_item(1, visible=False))),
            "大厅已下架",
        )
        self.assertEqual(participation_reason({}), "供应商大厅未收录")
        self.assertEqual(participation_reason(None), "供应商大厅未收录")
        for status in PARTICIPATION_EXCLUDED_STATUSES:
            self.assertIn(
                "模型检测未通过",
                participation_reason(hall_entry(hall_item(1, status=status))),
            )
        # 状态缺失同样不参与，避免未检测分组混入号池
        self.assertIn(
            "未检测",
            participation_reason(hall_entry(hall_item(1, status=""))),
        )

    def test_participation_ignores_detection_stage_details(self):
        # 即使基础探测阶段已完成，只要大厅判定不是 passed 就不参与
        entry = hall_entry(hall_item(
            1, status="detection_failed",
            stages={"detection": {"published_state": "completed"},
                    "text": {"published_state": "failed"}},
        ))
        self.assertIn("模型检测未通过", participation_reason(entry))

    def test_stage_disabled_families_maps_text_and_image(self):
        entry = hall_entry(hall_item(
            1, stages={"text": {"published_state": "failed"},
                       "image": {"published_state": "completed"},
                       "detection": {"published_state": "completed"}},
        ))
        families = stage_disabled_families(entry)
        self.assertIn("chat", families)
        self.assertIn("responses", families)
        self.assertNotIn("images", families)

        image_failed = hall_entry(hall_item(
            1, stages={"text": {"published_state": "completed"},
                       "image": {"published_state": "failed"}},
        ))
        self.assertIn("images", stage_disabled_families(image_failed))

    # ------------------------------------------------------------ 同步行为

    async def test_fetch_deletes_keys_of_removed_groups_only(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)],
            keys=[key(11, 1), key(21, 2), key(31, 3)],
            hall=[hall_item(1), hall_item(2, visible=False), hall_item(3)],
        )
        src = source()
        # 首次同步只登记不可用分组，不删除（需要连续两轮确认），但已下架分组
        # 立即退出路由
        _, entries = await adapter.fetch(client, src, session())
        self.assertEqual(client.deleted, [])
        self.assertEqual([entry["group_id"] for entry in entries], [1, 3])

        # 第二轮确认后才删除已下架分组的 Key
        _, entries = await adapter.fetch(client, src, session())
        self.assertEqual(client.deleted, ["21"])
        self.assertEqual([entry["group_id"] for entry in entries], [1, 3])

    async def test_fetch_removes_keys_of_groups_missing_from_hall(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(143)], keys=[key(11, 1), key(1431, 143)],
            # 143 只出现在 /groups/available，不在大厅快照里
            hall=[hall_item(1)],
        )
        src = source()

        _, entries = await adapter.fetch(client, src, session())
        self.assertEqual([entry["group_id"] for entry in entries], [1])
        self.assertEqual(client.deleted, [])

        _, entries = await adapter.fetch(client, src, session())
        self.assertEqual(client.deleted, ["1431"])
        self.assertEqual([entry["group_id"] for entry in entries], [1])

    async def test_fetch_keeps_only_passed_detection_groups(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)],
            keys=[key(11, 1), key(21, 2), key(31, 3)],
            hall=[hall_item(1), hall_item(2, status="suspected"),
                  hall_item(3, status="not_tested")],
        )
        src = source()

        _, entries = await adapter.fetch(client, src, session())
        self.assertEqual([entry["group_id"] for entry in entries], [1])
        self.assertEqual(sorted(src["group_model_cache"]), ["1"])

        _, entries = await adapter.fetch(client, src, session())
        self.assertEqual(sorted(client.deleted), ["21", "31"])
        self.assertEqual([entry["group_id"] for entry in entries], [1])
        self.assertEqual(sorted(src["group_model_cache"]), ["1"])

    async def test_fetch_does_not_delete_when_availability_flaps(self):
        adapter = AIHubAdapter()
        src = source()
        client = FakeAIHubClient(
            groups=[group(1)], keys=[key(11, 1)], hall=[hall_item(1, visible=False)],
        )
        await adapter.fetch(client, src, session())  # 第 1 轮：登记
        client.hall = [hall_item(1)]                 # 恢复可用
        await adapter.fetch(client, src, session())  # 第 2 轮：解除，不删
        self.assertEqual(client.deleted, [])

    async def test_fetch_keeps_keys_when_hall_cannot_be_read(self):
        adapter = AIHubAdapter()
        src = source()

        class FailingHallClient(FakeAIHubClient):
            async def get(self, url, params=None, headers=None, timeout=None):
                if url.endswith("/public/providers"):
                    return httpx.Response(503, text="<html>down</html>")
                return await super().get(url, params, headers, timeout)

        client = FailingHallClient(
            groups=[group(1)], keys=[key(11, 1)], hall=[hall_item(1, visible=False)],
        )

        with self.assertRaises(PoolSyncError) as raised:
            await adapter.fetch(client, src, session())

        self.assertIn("供应商大厅", str(raised.exception))
        self.assertEqual(client.deleted, [])

    async def test_fetch_uses_cached_hall_snapshot_after_failure(self):
        adapter = AIHubAdapter()
        src = source()
        src["hall_groups"] = {"1": hall_entry(hall_item(1, visible=False)),
                              "2": hall_entry(hall_item(2))}
        src["hall_blocked_pending"] = {"1": "大厅已下架"}

        class FailingHallClient(FakeAIHubClient):
            async def get(self, url, params=None, headers=None, timeout=None):
                if url.endswith("/public/providers"):
                    return httpx.Response(503, json={"code": 503, "message": "down"})
                return await super().get(url, params, headers, timeout)

        client = FailingHallClient(
            groups=[group(1), group(2)], keys=[key(11, 1), key(21, 2)], hall=[],
        )

        _, entries = await adapter.fetch(client, src, session())

        self.assertEqual(client.deleted, ["11"])
        self.assertEqual([entry["group_id"] for entry in entries], [2])

    # ------------------------------------------------------------ 能力合并

    async def test_fetch_rejects_unhealthy_models_only(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1)], keys=[key(11, 1)],
            hall=[hall_item(1, cards=[("gpt-6.1-sol", "failed"),
                                      ("gpt-6-astra", "healthy"),
                                      ("gpt-6-luna", "insufficient")])],
        )

        _, entries = await adapter.fetch(client, source(), session())

        capabilities = entries[0]["routing_capabilities"]
        self.assertEqual(capabilities["rejected_models"], ["gpt-6.1-sol"])
        # insufficient 不是降智证据，保留可用
        self.assertNotIn("gpt-6-luna", capabilities["rejected_models"])

    async def test_fetch_disables_image_endpoint_when_image_stage_failed(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1)], keys=[key(11, 1)],
            hall=[hall_item(1, stages={
                "text": {"published_state": "completed"},
                "image": {"published_state": "failed"},
                "detection": {"published_state": "completed"},
            })],
        )

        _, entries = await adapter.fetch(client, source(), session())

        capabilities = entries[0]["routing_capabilities"]
        self.assertFalse(capabilities["image_generation"])
        self.assertNotIn("images", capabilities["endpoint_families"])
        self.assertIn("chat", capabilities["endpoint_families"])

    # ------------------------------------------------------------ 分组目录

    async def test_catalog_lists_only_passed_detection_groups(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3), group(4), group(5), group(6)],
            keys=[key(11, 1), key(31, 3)],
            hall=[
                hall_item(1, rate=0.05, cache_hit=0.9),
                hall_item(2, visible=False),
                hall_item(3, status="detection_failed"),
                hall_item(4, status="suspected"),
                hall_item(5, status="not_tested"),
                # 组 6 不在大厅快照中
            ],
            rates={"1": 0.05},
        )

        _, catalog = await adapter.catalog(client, source(), session())

        self.assertEqual([item["id"] for item in catalog], [1])
        self.assertEqual(catalog[0]["rate_multiplier"], "0.05")
        self.assertLess(float(catalog[0]["effective_multiplier"]), 0.05)
        self.assertTrue(catalog[0]["hall_available"])

    async def test_only_missing_skips_blocked_groups(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)],
            keys=[key(11, 1)],
            hall=[hall_item(1), hall_item(2, visible=False), hall_item(3)],
        )

        _, result = await adapter.create_keys(
            client, source(), session(), [], only_missing=True,
        )

        self.assertEqual(client.created, [3])
        self.assertEqual(result["requested"], 1)

    async def test_delete_keys_is_throttled(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)],
            keys=[key(11, 1), key(21, 2), key(31, 3)],
            hall=[hall_item(1), hall_item(2), hall_item(3)],
        )
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", fake_sleep):
            _session, deleted = await adapter._delete_keys(
                client, source(), session(), [
                    {"id": 11}, {"id": 21}, {"id": 31},
                ], {"delay_seconds": 0.7},
            )

        self.assertEqual(deleted, 3)
        # 3 个删除 => 2 次间隔等待，第一次不等待
        self.assertEqual(sleeps, [0.7, 0.7])
        self.assertEqual(sorted(client.deleted), ["11", "21", "31"])

    async def test_delete_keys_stops_on_rate_limit(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1)], keys=[], hall=[hall_item(1)],
        )
        calls = []

        async def failing_delete(url, headers=None, timeout=None):
            calls.append(url)
            if len(calls) == 2:
                return httpx.Response(429, json={"message": "too many requests"},
                                      request=httpx.Request("DELETE", url))
            return api_response({})

        client.delete = failing_delete

        async def fake_sleep(seconds):
            return None

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", fake_sleep):
            _, deleted = await adapter._delete_keys(
                client, source(), session(),
                [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
            )

        # 第 2 个命中 429 后立刻停止，不再继续打第 3、4 个
        self.assertEqual(deleted, 1)
        self.assertEqual(len(calls), 2)

    async def test_create_keys_is_throttled(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)],
            keys=[], hall=[hall_item(1), hall_item(2), hall_item(3)],
        )
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", fake_sleep):
            _, result = await adapter.create_keys(
                client, source(), session(), [], only_missing=True,
                options={"delay_seconds": 1.2},
            )

        self.assertEqual(result["requested"], 3)
        self.assertEqual(sleeps, [1.2, 1.2])
        self.assertFalse(result["throttled"])

    async def test_create_keys_stops_immediately_on_rate_limit(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)],
            keys=[],
            hall=[hall_item(1), hall_item(2), hall_item(3)],
        )
        client.post_errors = {2: "上游请求失败 (HTTP 429 Too Many Requests)"}

        _, result = await adapter.create_keys(
            client, source(), session(), [], only_missing=True,
        )

        # 命中限流后不再尝试后续分组，避免放大限流窗口
        self.assertEqual(client.created, [1])
        self.assertNotIn(3, client.created)
        self.assertEqual(len(result["created"]), 1)
        messages = " ".join(str(item.get("error") or "") for item in result["errors"])
        self.assertIn("限流", messages)
        self.assertIn("剩余 1 个分组", messages)

    # ------------------------------------------------------- 同步时自动补 Key

    async def test_auto_create_is_off_by_default(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2)], keys=[key(11, 1)],
            hall=[hall_item(1), hall_item(2)],
        )

        await adapter.fetch(client, source(), session())

        self.assertEqual(client.created, [])

    async def test_auto_create_fills_missing_groups_by_effective_cost(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3), group(4)],
            keys=[key(11, 1), key(41, 4, status="disabled")],
            hall=[
                hall_item(1, rate=0.05),
                hall_item(2, rate=0.3, effective=0.3),
                hall_item(3, rate=0.2, effective=0.02),
                hall_item(4, rate=0.4, effective=0.4),
            ],
        )
        state = source()
        state["auto_create_keys"] = True

        await adapter.fetch(client, state, session())

        # 组 1 已有启用 Key，组 4 只有停用 Key；其余按有效倍率从低到高
        self.assertEqual(client.created, [3, 2, 4])
        self.assertEqual(state["auto_create_pending"], 0)
        self.assertEqual(state["auto_create_last_created"], 3)

    async def test_auto_create_skips_blocked_and_inactive_groups(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3), group(4), group(5),
                    group(6, status="inactive")],
            keys=[],
            hall=[hall_item(1), hall_item(2, visible=False),
                  hall_item(3, status="detection_failed"),
                  hall_item(4, status="suspected"),
                  hall_item(5, status="not_tested"), hall_item(6)],
        )
        state = source()
        state["auto_create_keys"] = True

        await adapter.fetch(client, state, session())

        # 大厅下架、检测失败、疑似、未检测、非 active 的分组都不应建 Key
        self.assertEqual(client.created, [1])

    async def test_auto_create_skips_groups_missing_from_hall(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2)], keys=[], hall=[hall_item(1)],
        )
        state = source()
        state["auto_create_keys"] = True

        await adapter.fetch(client, state, session())

        self.assertEqual(client.created, [1])

    async def test_auto_create_limits_round_size(self):
        groups = [group(index) for index in range(1, 26)]
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=groups, keys=[], hall=[hall_item(item["id"]) for item in groups],
        )
        state = source()
        state["auto_create_keys"] = True

        with patch("retry_proxy.sync_adapters.base.asyncio.sleep", AsyncMock()):
            await adapter.fetch(client, state, session())

        self.assertEqual(len(client.created), AUTO_CREATE_MAX_PER_ROUND)
        self.assertEqual(
            state["auto_create_pending"], 25 - AUTO_CREATE_MAX_PER_ROUND,
        )

    async def test_auto_create_rate_limit_stops_and_cools_down(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1), group(2), group(3)], keys=[],
            hall=[hall_item(1, effective=0.01), hall_item(2, effective=0.02),
                  hall_item(3, effective=0.03)],
        )
        client.post_errors = {2: "上游请求失败 (HTTP 429 Too Many Requests)"}
        state = source()
        state["auto_create_keys"] = True

        await adapter.fetch(client, state, session())

        self.assertEqual(client.created, [1])
        # 限流后剩余分组留到下一轮，失败分组进入冷却
        self.assertEqual(state["auto_create_pending"], 2)
        self.assertIn("2", state["auto_create_attempts"])
        self.assertIn("限流", state["auto_create_last_error"])
        # 上游按账号限流，整轮自动补齐进入冷却
        self.assertGreater(
            state["auto_create_cooldown_until"], time.time(),
        )

        client.created.clear()
        await adapter.fetch(client, state, session())

        # 冷却期内不再请求上游
        self.assertEqual(client.created, [])
        self.assertEqual(state["auto_create_pending"], 2)

        # 冷却到期后：冷却中的组 2 仍跳过，只补组 3
        state["auto_create_cooldown_until"] = 0
        await adapter.fetch(client, state, session())
        self.assertEqual(client.created, [3])
        self.assertEqual(state["auto_create_pending"], 0)

    async def test_auto_create_cooldown_expires_and_clears(self):
        adapter = AIHubAdapter()
        client = FakeAIHubClient(
            groups=[group(1)], keys=[], hall=[hall_item(1)],
        )
        state = source()
        state["auto_create_keys"] = True
        state["auto_create_attempts"] = {
            "1": {"at": time.time(), "error": "too many api keys created recently"},
        }

        await adapter.fetch(client, state, session())

        # 分组级冷却仍然生效，不会重复建 Key
        self.assertEqual(client.created, [])

        state["auto_create_attempts"] = {}
        await adapter.fetch(client, state, session())

        self.assertEqual(client.created, [1])
        self.assertEqual(state["auto_create_cooldown_until"], 0)
