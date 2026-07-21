import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from retry_proxy.key_pool import KeyEntry, KeyPool
from retry_proxy.pool_sync import PoolSyncManager
from retry_proxy.routes import RouteRegistry
from retry_proxy.sync_adapters import PoolSyncError
from retry_proxy.sync_adapters.sub2api import Sub2APIAdapter


def response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://upstream.test"))


class FakeClient:
    def __init__(self):
        self.calls = []
        self.created = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        if url.endswith("/auth/login"):
            return response({"code": 0, "data": {
                "access_token": "access-1", "refresh_token": "refresh-1",
            }})
        if url.endswith("/auth/refresh"):
            return response({"code": 0, "data": {
                "access_token": "access-2", "refresh_token": "refresh-2",
            }})
        if url.endswith("/auth/logout"):
            return response({"code": 0, "data": {"message": "ok"}})
        if url.endswith("/keys"):
            self.created.append(json["group_id"])
            return response({"code": 0, "data": {
                "id": 100 + len(self.created), "key": f"sk-created-{json['group_id']}",
                "name": json["name"], "group_id": json["group_id"], "status": "active",
            }})
        raise AssertionError(url)

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        if url.endswith("/keys"):
            items = [
                {"id": 11, "key": "sk-secret-one", "name": "A011", "group_id": 2,
                 "status": "active", "group": {"id": 2, "name": "Team", "platform": "openai",
                                                    "status": "active", "rate_multiplier": 0.05}},
                {"id": 12, "key": "sk-disabled", "name": "disabled", "group_id": 2,
                 "status": "inactive"},
            ]
            if 3 in self.created:
                items.append({
                    "id": 103, "key": "sk-created-3", "name": "Empty",
                    "group_id": 3, "status": "active",
                    "group": {"id": 3, "name": "Empty", "platform": "openai",
                              "status": "active", "rate_multiplier": 0.08},
                })
            return response({"code": 0, "data": {"items": items, "total": len(items)}})
        if url.endswith("/groups/available"):
            return response({"code": 0, "data": [
                {"id": 2, "name": "Team", "platform": "openai", "status": "active",
                 "rate_multiplier": 0.05},
                {"id": 3, "name": "Empty", "platform": "openai", "status": "active",
                 "rate_multiplier": 0.08},
            ]})
        if url.endswith("/groups/rates"):
            return response({"code": 0, "data": {"2": 0.03}})
        raise AssertionError(url)

    async def delete(self, url, headers=None, timeout=None):
        self.calls.append(("DELETE", url, None, headers))
        if "/keys/" in url:
            return response({"code": 0, "data": {}})
        raise AssertionError(url)


class QueuedProbeClient:
    def __init__(self):
        self.calls = []
        self.first_batch_ready = asyncio.Event()
        self.release_first_batch = asyncio.Event()

    async def post(self, url, json=None, headers=None, timeout=None):
        index = len(self.calls)
        self.calls.append((url, json, headers, timeout))
        if index < 2:
            if index == 1:
                self.first_batch_ready.set()
            await self.release_first_batch.wait()
        return response({"choices": []}, 200)


class Sub2APIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_and_fetch_normalize_keys_and_custom_rates(self):
        adapter = Sub2APIAdapter()
        client = FakeClient()
        source = {"base_url": "https://upstream.test"}

        session = await adapter.connect(client, source, {"email": "user@example.com", "password": "secret"})
        session, entries = await adapter.fetch(client, source, session)

        self.assertEqual(session["refresh_token"], "refresh-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], "sk-secret-one")
        self.assertEqual(entries[0]["label"], "A011-Team")
        self.assertEqual(entries[0]["sort"], "0.03")
        self.assertEqual(entries[0]["platform"], "openai")
        self.assertEqual(
            entries[0]["routing_capabilities"]["endpoint_families"],
            ["audio", "chat", "embeddings", "responses"],
        )

    def test_sub2api_routing_capabilities_use_structured_group_fields(self):
        capabilities = Sub2APIAdapter.routing_capabilities({
            "platform": "antigravity",
            "allow_image_generation": True,
            "supported_model_scopes": ["claude", "gemini_image"],
            "models_list_config": {"enabled": True, "models": ["claude-*", "gemini-*"]},
        })

        self.assertEqual(capabilities["platform"], "antigravity")
        self.assertEqual(capabilities["endpoint_families"], [
            "chat", "gemini", "images", "messages",
        ])
        self.assertEqual(capabilities["model_scopes"], ["claude", "gemini_image"])
        self.assertEqual(capabilities["model_patterns"], ["claude-*", "gemini-*"])

    def test_unknown_platform_omits_automatic_routing(self):
        self.assertEqual(Sub2APIAdapter.routing_capabilities({"platform": "future"}), {})

    async def test_expired_access_token_rotates_refresh_token(self):
        adapter = Sub2APIAdapter()
        client = FakeClient()
        original_get = client.get
        first = True

        async def unauthorized_once(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                return response({"code": "UNAUTHORIZED", "message": "expired"}, 401)
            return await original_get(*args, **kwargs)

        client.get = unauthorized_once
        session = {"email": "user@example.com", "access_token": "expired", "refresh_token": "refresh-1"}
        session, _ = await adapter.fetch(client, {"base_url": "https://upstream.test"}, session)

        self.assertEqual(session["access_token"], "access-2")
        self.assertEqual(session["refresh_token"], "refresh-2")


class PoolSyncManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tempdir.name, "sync.json")
        self.config = SimpleNamespace(
            key_pool_sync_state_file=self.state_file,
            key_pool_sync_default_adapter="sub2api",
            key_pool_sync_default_url="https://upstream.test",
            key_pool_sync_interval=0,
            provider="test-provider",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    async def test_connect_hot_reloads_pool_preserves_rules_and_hides_secrets(self):
        existing = KeyPool([])
        existing.entries = [KeyEntry("sk-secret-one", "old", models=("gpt-image-*",), paths=("images/*",))]
        existing.finalize_entries()
        existing.entries[0].total_fail = 3
        pools = {"https://upstream.test": existing}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})

        status = await manager.connect("sub2api", "https://upstream.test", "custom-provider", {
            "email": "user@example.com", "password": "not-persisted",
        })

        entry = pools["https://upstream.test"].entries[0]
        self.assertEqual(entry.label, "A011-Team")
        self.assertEqual(entry.sort, "0.03")
        self.assertEqual(entry.models, ("gpt-image-*",))
        self.assertEqual(entry.paths, ("images/*",))
        self.assertEqual(entry.total_fail, 3)
        self.assertEqual(pools["https://upstream.test"].provider, "custom-provider")
        public_key = status["sources"][0]["keys"][0]
        self.assertNotIn("sk-secret-one", json.dumps(status))
        self.assertEqual(public_key["key_masked"], "sk-secr...-one")

        with open(self.state_file, encoding="utf-8") as f:
            persisted = f.read()
        self.assertNotIn("not-persisted", persisted)
        self.assertNotIn("access-1", persisted)
        self.assertIn("refresh-1", persisted)
        if os.name != "nt":
            self.assertEqual(os.stat(self.state_file).st_mode & 0o777, 0o600)

    async def test_managed_route_is_persisted_and_restored(self):
        route_config = SimpleNamespace(
            extra_upstreams="", upstream_url="https://default.test", provider="default",
        )
        registry = RouteRegistry(route_config)
        manager = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()}, registry,
        )

        status = await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "secret"}, "/custom",
        )

        self.assertEqual(status["sources"][0]["route_prefix"], "/custom")
        self.assertEqual(registry.match("custom/v1/models")[0], "https://upstream.test")

        restored_registry = RouteRegistry(route_config)
        restored = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()}, restored_registry,
        )
        restored.load_state()

        self.assertEqual(restored.status()["sources"][0]["route_prefix"], "/custom")
        self.assertEqual(restored_registry.match("custom/v1/models")[0], "https://upstream.test")

    async def test_delete_removes_pool_source_managed_route_and_persisted_state(self):
        route_config = SimpleNamespace(
            extra_upstreams="", upstream_url="https://default.test", provider="default",
        )
        registry = RouteRegistry(route_config)
        client = FakeClient()
        pools = {}
        manager = PoolSyncManager(
            pools, self.config, client, {"sub2api": Sub2APIAdapter()}, registry,
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "secret"}, "/custom",
        )
        source_id = status["sources"][0]["id"]
        manager.operations[source_id] = {"kind": "create", "running": False}

        status = await manager.delete(source_id)

        self.assertEqual(status["sources"], [])
        self.assertNotIn("https://upstream.test", pools)
        self.assertNotIn(source_id, manager.operations)
        self.assertEqual(registry.match("custom/v1/models")[0], "https://default.test")
        self.assertTrue(any(call[0] == "POST" and call[1].endswith("/auth/logout")
                            for call in client.calls))
        with open(self.state_file, encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["sources"], [])

        restored = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
            RouteRegistry(route_config),
        )
        restored.load_state()
        self.assertEqual(restored.status()["sources"], [])

    async def test_delete_restores_static_pool_for_the_same_upstream(self):
        client = FakeClient()
        static = KeyPool([("static-key", "static")], "static-provider")
        pools = {"https://upstream.test": static}
        manager = PoolSyncManager(
            pools, self.config, client, {"sub2api": Sub2APIAdapter()},
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "online-provider",
            {"email": "user@example.com", "password": "secret"},
        )
        source_id = status["sources"][0]["id"]
        self.assertEqual(pools["https://upstream.test"].entries[0].key, "sk-secret-one")

        await manager.delete(source_id)

        restored = pools["https://upstream.test"]
        self.assertEqual(restored.provider, "static-provider")
        self.assertEqual([entry.key for entry in restored.entries], ["static-key"])

    async def test_same_provider_online_source_overrides_route_and_pool_then_restores_static(self):
        test_url = "http://57.131.13.16:8080"
        production_url = "https://upstream.test"
        route_config = SimpleNamespace(
            extra_upstreams=f"/aihub|{test_url}|aihub",
            upstream_url="https://default.test", provider="default",
        )
        registry = RouteRegistry(route_config)
        static = KeyPool([("static-key", "static")], "aihub")
        pools = {test_url: static}
        manager = PoolSyncManager(
            pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()}, registry,
        )

        status = await manager.connect(
            "sub2api", production_url, "aihub",
            {"email": "user@example.com", "password": "secret"}, "/aihub",
        )
        source_id = status["sources"][0]["id"]

        self.assertEqual(manager.sources[source_id]["pool_url"], production_url)
        self.assertEqual(registry.match("aihub/responses")[0], production_url)
        self.assertEqual([entry.key for entry in pools[production_url].entries], ["sk-secret-one"])
        self.assertEqual([entry.key for entry in pools[test_url].entries], ["static-key"])

        restored_pools = {test_url: KeyPool([("static-key", "static")], "aihub")}
        restored_registry = RouteRegistry(route_config)
        restored = PoolSyncManager(
            restored_pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
            restored_registry,
        )
        restored.load_state()
        self.assertEqual(
            [entry.key for entry in restored_pools[production_url].entries], ["sk-secret-one"],
        )
        self.assertEqual([entry.key for entry in restored_pools[test_url].entries], ["static-key"])
        self.assertEqual(restored_registry.match("aihub/responses")[0], production_url)

        await restored.delete(source_id)

        self.assertNotIn(production_url, restored_pools)
        self.assertEqual([entry.key for entry in restored_pools[test_url].entries], ["static-key"])
        self.assertEqual(restored_registry.match("aihub/responses")[0], test_url)

    async def test_disconnect_keeps_source_route_and_last_synced_pool(self):
        route_config = SimpleNamespace(
            extra_upstreams="", upstream_url="https://default.test", provider="default",
        )
        registry = RouteRegistry(route_config)
        pools = {}
        manager = PoolSyncManager(
            pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()}, registry,
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "online-provider",
            {"email": "user@example.com", "password": "secret"}, "/custom",
        )
        source_id = status["sources"][0]["id"]

        status = await manager.disconnect(source_id)

        self.assertEqual(len(status["sources"]), 1)
        self.assertFalse(status["sources"][0]["connected"])
        self.assertIn("https://upstream.test", pools)
        self.assertEqual(registry.match("custom/responses")[0], "https://upstream.test")
        with open(self.state_file, encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(len(persisted["sources"]), 1)
        self.assertEqual(persisted["sources"][0]["session"], {})

    async def test_managed_route_rejects_root_prefix(self):
        route_config = SimpleNamespace(
            extra_upstreams="", upstream_url="https://default.test", provider="default",
        )
        registry = RouteRegistry(route_config)
        manager = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()}, registry,
        )

        with self.assertRaisesRegex(PoolSyncError, "代理前缀不能为空"):
            await manager.connect(
                "sub2api", "https://upstream.test", "provider",
                {"email": "user@example.com", "password": "secret"}, "/",
            )

    async def test_legacy_source_uses_matching_environment_route(self):
        state = {"version": 2, "sources": [{
            "id": "legacy", "adapter": "sub2api", "base_url": "https://upstream.test",
            "provider": "legacy-provider", "session": {}, "entries": [],
        }]}
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
        route_config = SimpleNamespace(
            extra_upstreams="/legacy|https://upstream.test|env-provider",
            upstream_url="https://default.test", provider="default",
        )
        registry = RouteRegistry(route_config)
        manager = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()}, registry,
        )

        manager.load_state()

        self.assertEqual(manager.status()["sources"][0]["route_prefix"], "/legacy")
        self.assertEqual(registry.match("legacy/models")[:2], ("https://upstream.test", "env-provider"))
        self.assertEqual(manager.sources["legacy"]["route_prefix"], "")

    async def test_state_restores_multiple_generic_sources(self):
        state = {"version": 2, "sources": [
            {"id": "one", "adapter": "sub2api", "base_url": "https://one.test",
             "provider": "one", "session": {"email": "a@b.c", "refresh_token": "r"},
             "entries": [{"key": "key-one", "label": "one", "sort": "0.1",
                          "models": [], "paths": []}]},
            {"id": "two", "adapter": "sub2api", "base_url": "https://two.test",
             "provider": "two", "session": {},
             "entries": [{"key": "key-two", "label": "two", "sort": "0.2",
                          "models": [], "paths": []}]},
        ]}
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
        pools = {}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})

        manager.load_state()

        self.assertEqual(set(pools), {"https://one.test", "https://two.test"})
        self.assertEqual(len(manager.status()["sources"]), 2)

    async def test_state_restores_authoritative_empty_pool(self):
        state = {"version": 2, "sources": [
            {"id": "empty", "adapter": "sub2api", "base_url": "https://upstream.test",
             "provider": "test", "session": {"refresh_token": "r"}, "entries": [],
             "last_sync_at": "2026-07-17T00:00:00+00:00"},
        ]}
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
        pools = {"https://upstream.test": KeyPool(["stale-key"])}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})

        manager.load_state()

        self.assertEqual(pools["https://upstream.test"].entries, [])

    async def test_interval_is_persisted_and_restored(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})

        status = await manager.set_interval(900)

        self.assertEqual(status["interval"], 900)
        restored_config = SimpleNamespace(
            key_pool_sync_state_file=self.state_file,
            key_pool_sync_default_adapter="sub2api",
            key_pool_sync_default_url="https://upstream.test",
            key_pool_sync_interval=60,
            provider="test-provider",
        )
        restored = PoolSyncManager({}, restored_config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        restored.load_state()
        self.assertEqual(restored_config.key_pool_sync_interval, 900)

    async def test_source_strategy_is_applied_and_persisted(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        pool = manager.pools["https://upstream.test"]
        pool._selection_count = 19
        view = pool.for_request("test-model", "v1/chat/completions")
        view._selection_count = 19

        status = await manager.set_source_settings(
            source_id, "balanced", 4.5, "test-model",
        )

        source = status["sources"][0]
        self.assertEqual(source["strategy"], "balanced")
        self.assertEqual(source["target_ttft_s"], 4.5)
        self.assertEqual(source["check_model"], "test-model")
        self.assertEqual(pool.strategy, "balanced")
        self.assertEqual(pool.target_ttft_s, 4.5)
        self.assertEqual(pool._selection_count, 0)
        self.assertEqual(view._selection_count, 0)
        with open(self.state_file, encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["sources"][0]["strategy"], "balanced")
        self.assertEqual(persisted["sources"][0]["check_model"], "test-model")

    async def test_availability_check_cools_failed_group_and_reset_clears_it(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        manager.client.post = AsyncMock(return_value=response({"error": "unavailable"}, 503))
        self.config.key_auth_header = "authorization"
        self.config.key_auth_scheme = "Bearer"
        self.config.key_cooldown_5xx = 30

        result = await manager.check_availability(source_id, "test-model")

        self.assertFalse(result["checks"][0]["available"])
        self.assertTrue(result["checks"][0]["circuit_opened"])
        self.assertEqual(result["checks"][0]["reason"], "upstream_error")
        entry = manager.pools["https://upstream.test"].entries[0]
        self.assertTrue(entry.cooldown_until > 0)
        self.assertEqual(entry.last_failure_kind, "probe")
        call = manager.client.post.await_args
        self.assertTrue(call.args[0].endswith("/v1/chat/completions"))
        self.assertEqual(call.kwargs["json"]["model"], "test-model")
        self.assertEqual(call.kwargs["json"]["max_tokens"], 1)

        await manager.reset_group(source_id, entry.group_id)
        self.assertEqual(entry.cooldown_until, 0)

        manager.client.post = AsyncMock(return_value=response({"choices": []}, 200))
        result = await manager.check_availability(source_id)
        self.assertTrue(result["checks"][0]["available"])
        self.assertEqual(entry.ttft_samples, 1)

    async def test_availability_check_does_not_cool_model_rejection(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        pool = manager.pools["https://upstream.test"]
        pool.entries.append(KeyEntry(
            "sk-secret-two", "second", sort="0.03", group_id="2", group_name="Team",
        ))
        pool.finalize_entries()
        manager.client.post = AsyncMock(return_value=response({
            "error": {"type": "model_not_found", "message": "unsupported"},
        }, 404))
        self.config.key_auth_header = "authorization"
        self.config.key_auth_scheme = "Bearer"
        self.config.key_cooldown_5xx = 30

        with self.assertLogs("forward", level="INFO") as captured:
            result = await manager.check_availability(source_id, "missing-model")

        check = result["checks"][0]
        self.assertFalse(check["available"])
        self.assertFalse(check["circuit_opened"])
        self.assertEqual(check["reason"], "request_rejected")
        self.assertEqual(check["statuses"], [404])
        self.assertEqual(manager.client.post.await_count, 1)
        self.assertTrue(all(entry.cooldown_until == 0 for entry in pool.entries))
        self.assertIn("model=missing-model", "\n".join(captured.output))
        self.assertIn("statuses=404:1", "\n".join(captured.output))

    async def test_availability_check_stops_group_after_first_success(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        pool = manager.pools["https://upstream.test"]
        pool.entries.append(KeyEntry(
            "sk-secret-two", "second", sort="0.03", group_id="2", group_name="Team",
        ))
        pool.finalize_entries()
        manager.client.post = AsyncMock(return_value=response({"choices": []}, 200))
        self.config.key_auth_header = "authorization"
        self.config.key_auth_scheme = "Bearer"

        result = await manager.check_availability(source_id, "test-model")

        self.assertTrue(result["checks"][0]["available"])
        self.assertEqual(manager.client.post.await_count, 1)

    async def test_availability_ttft_excludes_concurrency_queue_time(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        pool = manager.pools["https://upstream.test"]
        pool.entries = [
            KeyEntry(
                f"sk-secret-{index}", f"group-{index}", sort=str(index),
                group_id=str(index), group_name=f"group-{index}",
            )
            for index in range(3)
        ]
        pool.finalize_entries()
        client = QueuedProbeClient()
        manager.client = client
        self.config.key_auth_header = "authorization"
        self.config.key_auth_scheme = "Bearer"

        task = asyncio.create_task(manager.check_availability(source_id, "test-model"))
        await client.first_batch_ready.wait()
        await asyncio.sleep(0.05)
        client.release_first_batch.set()
        result = await task

        checks = {item["group_id"]: item for item in result["checks"]}
        first_batch_min = min(checks[str(index)]["ttft_s"] for index in range(2))
        self.assertLess(checks["2"]["ttft_s"], first_batch_min / 2)

    async def test_catalog_and_one_click_create_only_missing_groups(self):
        client = FakeClient()
        pools = {"https://upstream.test": KeyPool([])}
        manager = PoolSyncManager(pools, self.config, client, {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]

        catalog = await manager.catalog(source_id)
        counts = {group["id"]: group["key_count"] for group in catalog["groups"]}
        self.assertEqual(counts, {2: 2, 3: 0})

        result = await manager.create_keys(source_id, only_missing=True)

        self.assertEqual(client.created, [3])
        self.assertEqual(result["creation"]["created"][0]["group_name"], "Empty")
        self.assertEqual(len(pools["https://upstream.test"].entries), 2)
        create_call = next(call for call in client.calls
                           if call[0] == "POST" and call[1].endswith("/keys"))
        self.assertEqual(create_call[2]["name"], "Empty")
        self.assertTrue(create_call[3]["Idempotency-Key"].startswith("pool-sync-key-"))

    async def test_group_rules_apply_to_synced_keys(self):
        client = FakeClient()
        pools = {"https://upstream.test": KeyPool([])}
        manager = PoolSyncManager(pools, self.config, client, {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]

        status = await manager.set_group_rules(source_id, {
            "2": {"models": "image2-*", "paths": "v1/images/*"},
        })

        key = next(item for item in status["sources"][0]["keys"] if item["group_name"] == "Team")
        self.assertEqual(key["models"], ["image2-*"])
        self.assertEqual(key["paths"], ["v1/images/*"])
        self.assertEqual(pools["https://upstream.test"].entries[0].models, ("image2-*",))
        self.assertEqual(
            pools["https://upstream.test"].entries[0].routing_capabilities["platform"],
            "openai",
        )

    async def test_reset_key_clears_runtime_circuit_breaker(self):
        pools = {"https://upstream.test": KeyPool([])}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        pool = pools["https://upstream.test"]
        entry = pool.entries[0]
        pool.mark_cooldown(entry, 1800, failure_kind="auth", status=403)

        status = await manager.reset_key(source_id, 11)

        visible = next(item for item in status["sources"][0]["keys"] if item["source_key_id"] == 11)
        self.assertFalse(visible["cooled"])
        self.assertEqual(visible["cooldown_remaining"], 0)
        self.assertEqual(entry.cooldown_until, 0)
        self.assertEqual(entry.consecutive_failures, 0)
        self.assertEqual(entry.total_fail, 1)

    async def test_clear_selected_groups_deletes_remote_keys_and_resyncs(self):
        client = FakeClient()
        pools = {"https://upstream.test": KeyPool([])}
        manager = PoolSyncManager(pools, self.config, client, {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })

        result = await manager.clear_keys(status["sources"][0]["id"], [2])

        self.assertEqual(len(result["deletion"]["deleted"]), 2)
        self.assertEqual(
            [call[1] for call in client.calls if call[0] == "DELETE"],
            ["https://upstream.test/api/v1/keys/11", "https://upstream.test/api/v1/keys/12"],
        )

    async def test_zero_key_upstream_connects_with_an_authoritative_empty_pool(self):
        client = FakeClient()
        original_get = client.get

        async def no_keys(url, params=None, headers=None, timeout=None):
            if url.endswith("/keys"):
                return response({"code": 0, "data": {"items": [], "total": 0}})
            return await original_get(url, params, headers, timeout)

        client.get = no_keys
        pools = {"https://upstream.test": KeyPool(["stale-key"])}
        manager = PoolSyncManager(pools, self.config, client, {"sub2api": Sub2APIAdapter()})

        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })

        self.assertEqual(status["sources"][0]["key_count"], 0)
        self.assertEqual(pools["https://upstream.test"].entries, [])

    def test_single_existing_pool_is_used_as_generic_default_url(self):
        self.config.key_pool_sync_default_url = "https://default-without-pool.test"
        manager = PoolSyncManager(
            {"https://configured-pool.test": KeyPool(["key"])}, self.config,
            FakeClient(), {"sub2api": Sub2APIAdapter()},
        )

        self.assertEqual(manager.status()["defaults"]["base_url"], "https://configured-pool.test")


if __name__ == "__main__":
    unittest.main()
