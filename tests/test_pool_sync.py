import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx

from retry_proxy.key_pool import KeyEntry, KeyPool
from retry_proxy.pool_sync import (PoolSyncManager, _get_pinned_public_url,
                                   _parse_experience_payload,
                                   _validate_base_url_destination)
from retry_proxy.routes import RouteRegistry
from retry_proxy.sync_adapters import PoolSyncError
from retry_proxy.sync_adapters.sub2api import Sub2APIAdapter, _model_ids, _unwrap


def response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://upstream.test"))


def text_response(body, status=200, headers=None):
    return httpx.Response(
        status, text=body, headers=headers,
        request=httpx.Request("GET", "https://upstream.test"),
    )


class FakeClient:
    def __init__(self):
        self.calls = []
        self.created = []
        self.fail_models = False

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
        if url.endswith("/v1/models"):
            if self.fail_models:
                return response({"error": {"message": "temporarily unavailable"}}, 503)
            key = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            models = {
                "sk-secret-one": ["gpt-5.4", "gpt-4.1"],
                "sk-created-3": ["gpt-5.4-mini"],
            }.get(key, [])
            return response({
                "object": "list", "data": [
                    {"id": model, "object": "model"} for model in models
                ],
            })
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
    def test_model_ids_parse_openai_and_gemini_shapes(self):
        self.assertEqual(_model_ids({"data": [
            {"id": "gpt-5.4"}, {"id": "gpt-5.4"},
        ]}), ["gpt-5.4"])
        self.assertEqual(_model_ids({"models": [
            {"name": "models/gemini-2.5-pro"},
        ]}), ["gemini-2.5-pro"])

    def test_non_json_cloudflare_403_has_actionable_message_without_body(self):
        upstream = text_response(
            "<html>request id secret-response-body</html>", 403,
            {
                "content-type": "text/html; charset=UTF-8",
                "server": "cloudflare",
                "cf-ray": "ray-id",
            },
        )

        with self.assertRaises(PoolSyncError) as raised:
            _unwrap(upstream)

        message = str(raised.exception)
        self.assertIn("Cloudflare/CDN", message)
        self.assertIn("HTTP 403", message)
        self.assertIn("站点根地址", message)
        self.assertNotIn("secret-response-body", message)

    def test_non_json_html_403_identifies_rejection_page(self):
        upstream = text_response(
            "<html><title>Forbidden</title></html>", 403,
            {"content-type": "text/html"},
        )

        with self.assertRaisesRegex(PoolSyncError, "HTML 拒绝页.*HTTP 403"):
            _unwrap(upstream)

    async def test_connect_and_fetch_normalize_keys_and_custom_rates(self):
        adapter = Sub2APIAdapter()
        client = FakeClient()
        source = {"base_url": "https://upstream.test"}

        session = await adapter.connect(client, source, {"email": "user@example.com", "password": "secret"})
        session, entries = await adapter.fetch(client, source, session)

        self.assertEqual(client.calls[0][3]["Accept"], "application/json")
        self.assertEqual(client.calls[1][3]["Accept"], "application/json")
        self.assertEqual(client.calls[1][3]["Authorization"], "Bearer access-1")
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
        self.assertEqual(
            entries[0]["routing_capabilities"]["model_patterns"],
            ["gpt-5.4", "gpt-4.1"],
        )
        self.assertTrue(entries[0]["routing_capabilities"]["model_list_known"])
        self.assertEqual(source["group_model_cache"]["2"], ["gpt-5.4", "gpt-4.1"])

    async def test_fetch_requests_models_once_per_group(self):
        adapter = Sub2APIAdapter()
        client = FakeClient()
        source = {"base_url": "https://upstream.test"}
        session = {"access_token": "access-1", "refresh_token": "refresh-1"}

        _, first = await adapter.fetch(client, source, session)
        _, second = await adapter.fetch(client, source, session)

        model_calls = [call for call in client.calls if call[1].endswith("/v1/models")]
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(
            first[0]["routing_capabilities"]["model_patterns"],
            second[0]["routing_capabilities"]["model_patterns"],
        )

    async def test_failed_model_request_retries_on_next_sync(self):
        adapter = Sub2APIAdapter()
        client = FakeClient()
        client.fail_models = True
        source = {"base_url": "https://upstream.test"}
        session = {"access_token": "access-1", "refresh_token": "refresh-1"}

        await adapter.fetch(client, source, session)
        client.fail_models = False
        _, entries = await adapter.fetch(client, source, session)

        model_calls = [call for call in client.calls if call[1].endswith("/v1/models")]
        self.assertEqual(len(model_calls), 2)
        self.assertEqual(source["group_model_cache"]["2"], ["gpt-5.4", "gpt-4.1"])
        self.assertTrue(entries[0]["routing_capabilities"]["model_list_known"])

    async def test_sync_deletes_orphaned_group_key_and_hides_group_from_catalog(self):
        adapter = Sub2APIAdapter()
        client = FakeClient()
        original_get = client.get

        async def orphaned_group(url, params=None, headers=None, timeout=None):
            if url.endswith("/keys"):
                return response({"code": 0, "data": {"items": [{
                    "id": 91, "key": "sk-orphaned", "name": "legacy",
                    "group_id": 9, "status": "active",
                    "group": {"id": 9, "name": "已删除套餐", "status": "inactive"},
                }], "total": 1}})
            return await original_get(url, params, headers, timeout)

        client.get = orphaned_group
        source = {"base_url": "https://upstream.test", "id": "source-1"}
        session = {"access_token": "access-1", "refresh_token": "refresh-1"}

        _, entries = await adapter.fetch(client, source, session)
        _, catalog = await adapter.catalog(client, source, session)

        self.assertEqual(entries, [])
        self.assertNotIn("9", {str(group["id"]) for group in catalog})
        self.assertIn(
            "https://upstream.test/api/v1/keys/91",
            [call[1] for call in client.calls if call[0] == "DELETE"],
        )

    async def test_repeated_key_page_raises_instead_of_looping_forever(self):
        # Regression: a misbehaving upstream returning the same page each time
        # used to loop without bound, holding the sync lock. Now it raises.
        adapter = Sub2APIAdapter()

        class RepeatingKeyClient:
            async def get(self, url, params=None, headers=None, timeout=None):
                page = (params or {}).get("page", 1)
                if url.endswith("/keys"):
                    # Always report total=200 and return a full page (100 items)
                    # of the SAME ids each time, so the loop would never end
                    # without the duplicate-page guard.
                    page_items = [
                        {"id": 100 + i, "key": f"sk-{i}", "name": f"A{i}",
                         "group_id": 2, "status": "active",
                         "group": {"id": 2, "name": "Team", "platform": "openai",
                                   "status": "active", "rate_multiplier": 0.05}}
                        for i in range(100)
                    ]
                    return response({"code": 0, "data": {
                        "items": page_items, "total": 200}})
                if url.endswith("/groups/available"):
                    return response({"code": 0, "data": [
                        {"id": 2, "name": "Team", "platform": "openai",
                         "status": "active", "rate_multiplier": 0.05}]})
                if url.endswith("/groups/rates"):
                    return response({"code": 0, "data": {"2": 0.03}})
                if url.endswith("/v1/models"):
                    return response({"object": "list", "data": []})
                raise AssertionError(url)

        source = {"base_url": "https://upstream.test"}
        session = {"access_token": "access-1", "refresh_token": "refresh-1"}
        with self.assertRaises(PoolSyncError) as raised:
            await adapter.fetch(RepeatingKeyClient(), source, session)
        self.assertIn("分页重复", str(raised.exception))

    async def test_non_numeric_total_raises_pool_sync_error(self):
        adapter = Sub2APIAdapter()

        class BadTotalClient:
            async def get(self, url, params=None, headers=None, timeout=None):
                if url.endswith("/keys"):
                    return response({"code": 0, "data": {
                        "items": [{"id": 1, "key": "k", "name": "n", "group_id": 2,
                                   "status": "active",
                                   "group": {"id": 2, "name": "Team", "platform": "openai",
                                             "status": "active", "rate_multiplier": 0.05}}],
                        "total": "not-a-number"}})
                if url.endswith("/groups/available"):
                    return response({"code": 0, "data": [
                        {"id": 2, "name": "Team", "platform": "openai",
                         "status": "active", "rate_multiplier": 0.05}]})
                if url.endswith("/groups/rates"):
                    return response({"code": 0, "data": {"2": 0.03}})
                if url.endswith("/v1/models"):
                    return response({"object": "list", "data": []})
                raise AssertionError(url)

        source = {"base_url": "https://upstream.test"}
        session = {"access_token": "access-1", "refresh_token": "refresh-1"}
        with self.assertRaises(PoolSyncError):
            await adapter.fetch(BadTotalClient(), source, session)

    def test_sub2api_routing_capabilities_use_structured_group_fields(self):
        capabilities = Sub2APIAdapter.routing_capabilities({
            "platform": "antigravity",
            "allow_image_generation": True,
            "supported_model_scopes": ["claude", "gemini_image"],            "models_list_config": {"enabled": True, "models": ["claude-*", "gemini-*"]},
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


class ExternalDataParserTests(unittest.TestCase):
    TRANSFORM = {
        "items_path": "rows", "id_path": "id", "ttft_path": "latency",
        "ttft_unit": "ms", "name_path": "", "platform_path": "",
        "rate_path": "", "samples_path": "", "timestamp_path": "",
    }

    def test_configurable_paths_normalize_external_rows(self):
        items = _parse_experience_payload({
            "result": {"rows": [{
                "identity": {"value": "remote-a"},
                "title": "Fast", "latency": 1.25, "count": "8",
                "observed": "2026-07-25T18:41:34Z",
            }]},
        }, {
            "items_path": "result.rows", "id_path": "identity.value",
            "name_path": "title", "platform_path": "", "rate_path": "",
            "ttft_path": "latency", "ttft_unit": "s",
            "samples_path": "count", "timestamp_path": "observed",
        })

        self.assertEqual(items[0]["id"], "remote-a")
        self.assertEqual(items[0]["name"], "Fast")
        self.assertEqual(items[0]["ttft"], 1.25)
        self.assertEqual(items[0]["samples"], 8)
        self.assertGreater(items[0]["last_ts"], 0)

    def test_top_level_array_and_missing_optional_fields_are_supported(self):
        items = _parse_experience_payload([
            {"key": "fast", "latency": 0.75},
        ], {
            "items_path": "$", "id_path": "key", "ttft_path": "latency",
            "ttft_unit": "s", "name_path": "", "platform_path": "",
            "rate_path": "", "samples_path": "", "timestamp_path": "",
        })

        self.assertEqual(items, [{
            "id": "fast", "name": "fast", "platform": "",
            "rate_multiplier": None, "ttft": 0.75, "samples": 1,
            "last_ts": 0.0,
        }])

    def test_generic_query_params_and_legacy_sample_param_are_supported(self):
        generic = PoolSyncManager._normalize_experience_source(
            "https://metrics.test/groups", transform=self.TRANSFORM,
            query_params={"limit": 50, "scope": "public"},
        )
        legacy = PoolSyncManager._normalize_experience_source(
            "https://metrics.test/groups", 50, "limit", self.TRANSFORM,
        )

        self.assertEqual(generic["query_params"], {
            "limit": "50", "scope": "public",
        })
        self.assertNotIn("sample_param", generic)
        self.assertEqual(legacy["sample_param"], "limit")
        self.assertEqual(legacy["samples"], 50)

    def test_private_or_loopback_experience_url_is_rejected(self):
        # Regression: SSRF mitigation -- private/loopback IP literals must be
        # blocked so an admin (or anyone reaching the admin API) cannot probe
        # internal services via the experience-data fetch.
        for blocked in (
            "http://127.0.0.1/groups", "http://10.0.0.1/groups",
            "http://169.254.169.254/latest/meta-data",
            "http://localhost/groups", "http://[::1]/groups",
        ):
            with self.subTest(url=blocked):
                with self.assertRaises(PoolSyncError) as raised:
                    PoolSyncManager._normalize_experience_source(blocked)
                self.assertIn("私有", str(raised.exception))

    def test_public_experience_url_is_accepted(self):
        config = PoolSyncManager._normalize_experience_source(
            "https://metrics.test/groups", transform=self.TRANSFORM,
        )
        self.assertEqual(config["url"], "https://metrics.test/groups")


class ExternalDataNetworkSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_resolving_to_private_address_is_rejected_before_fetch(self):
        client = SimpleNamespace(get=AsyncMock())
        manager = PoolSyncManager({}, SimpleNamespace(), client, {})
        loop = SimpleNamespace(getaddrinfo=AsyncMock(return_value=[
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]))
        config = {
            "url": "https://metrics.test/groups",
            "query_params": {},
            "transform": ExternalDataParserTests.TRANSFORM,
        }

        with patch("retry_proxy.pool_sync.asyncio.get_running_loop", return_value=loop), \
                self.assertRaisesRegex(PoolSyncError, "非公网"):
            await manager._fetch_experience_items(config)

        client.get.assert_not_awaited()

    async def test_validated_address_is_pinned_for_the_actual_https_request(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response({"ok": True})))

        class ClientContext:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_args):
                return False

        loop = SimpleNamespace(getaddrinfo=AsyncMock(return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]))
        with patch("retry_proxy.pool_sync.asyncio.get_running_loop", return_value=loop), \
                patch("retry_proxy.pool_sync.httpx.AsyncClient",
                      return_value=ClientContext()):
            await _get_pinned_public_url(
                "https://metrics.test/groups?scope=public",
                params={"limit": "50"}, headers={"Accept": "application/json"},
            )

        call = session.get.await_args
        self.assertEqual(call.args[0], "https://93.184.216.34/groups?scope=public")
        self.assertEqual(call.kwargs["headers"]["Host"], "metrics.test")
        self.assertEqual(call.kwargs["extensions"]["sni_hostname"], "metrics.test")

    async def test_pinned_ipv6_request_preserves_original_host_and_port(self):
        session = SimpleNamespace(get=AsyncMock(return_value=response({"ok": True})))

        class ClientContext:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_args):
                return False

        loop = SimpleNamespace(getaddrinfo=AsyncMock(return_value=[
            (10, 1, 6, "", ("2001:4860:4860::8888", 8443, 0, 0)),
        ]))
        with patch("retry_proxy.pool_sync.asyncio.get_running_loop", return_value=loop), \
                patch("retry_proxy.pool_sync.httpx.AsyncClient",
                      return_value=ClientContext()):
            await _get_pinned_public_url("https://[2001:4860:4860::8844]:8443/groups")

        call = session.get.await_args
        self.assertEqual(call.args[0], "https://[2001:4860:4860::8888]:8443/groups")
        self.assertEqual(call.kwargs["headers"]["Host"], "[2001:4860:4860::8844]:8443")
        self.assertEqual(call.kwargs["extensions"]["sni_hostname"], "2001:4860:4860::8844")


class BaseUrlSsfrValidationTests(unittest.IsolatedAsyncioTestCase):
    """connect / manual-add 的 base_url 反 SSRF 校验"""

    async def _assert_rejected(self, url, addrinfo=None):
        loop = SimpleNamespace(getaddrinfo=AsyncMock(return_value=addrinfo or []))
        with patch("retry_proxy.pool_sync.asyncio.get_running_loop", return_value=loop), \
                self.assertRaises(PoolSyncError):
            await _validate_base_url_destination(url)

    async def test_private_and_loopback_literals_are_rejected(self):
        for url in ("http://127.0.0.1:8080", "http://169.254.169.254/latest/meta-data",
                    "https://192.168.1.5", "http://10.0.0.1", "http://[::1]",
                    "https://localhost", "http://100.100.100.200"):
            with self.subTest(url=url):
                await self._assert_rejected(url)

    async def test_cloud_metadata_hostname_is_rejected(self):
        await self._assert_rejected("http://metadata.google.internal/")

    async def test_public_ip_literal_is_accepted(self):
        await _validate_base_url_destination("https://93.184.216.34/groups")

    async def test_hostname_resolving_to_private_address_is_rejected(self):
        await self._assert_rejected("https://internal.test", addrinfo=[
            (2, 1, 6, "", ("192.168.1.10", 443)),
        ])

    async def test_connect_rejects_private_base_url_by_default(self):
        config = SimpleNamespace(
            key_pool_sync_state_file="unused-state.json",
            key_pool_sync_default_adapter="sub2api",
            key_pool_sync_default_url="https://upstream.test",
            key_pool_sync_interval=0, key_pool_sync_secret="",
            provider="test-provider",
            key_pool_allow_private_base_url=False,
        )
        manager = PoolSyncManager({}, config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        with self.assertRaises(PoolSyncError) as raised:
            await manager.connect("sub2api", "http://169.254.169.254", "test", {})
        self.assertIn("私有", str(raised.exception))

    async def test_connect_allows_private_base_url_when_opt_in(self):
        config = SimpleNamespace(
            key_pool_sync_state_file="unused-state.json",
            key_pool_sync_default_adapter="sub2api",
            key_pool_sync_default_url="https://upstream.test",
            key_pool_sync_interval=0, key_pool_sync_secret="",
            provider="test-provider",
            key_pool_allow_private_base_url=True,
        )
        manager = PoolSyncManager({}, config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        with patch("retry_proxy.pool_sync._validate_base_url_destination",
                   new_callable=AsyncMock) as check, \
                patch.object(Sub2APIAdapter, "connect", new_callable=AsyncMock,
                             side_effect=PoolSyncError("adapter-boom")):
            with self.assertRaises(PoolSyncError) as raised:
                await manager.connect("sub2api", "http://192.168.1.10", "test", {})
        check.assert_not_awaited()
        self.assertIn("adapter-boom", str(raised.exception))

    async def test_manual_add_rejects_private_base_url_by_default(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter

        config = SimpleNamespace(
            key_pool_sync_state_file="unused-state.json",
            key_pool_sync_default_adapter="manual",
            key_pool_sync_default_url="https://manual.test",
            key_pool_sync_interval=0, key_pool_sync_secret="",
            provider="manual-provider", extra_upstreams="",
            upstream_url="https://default.test",
            key_pool_allow_private_base_url=False,
        )
        manager = PoolSyncManager({}, config, None, {"manual": ManualAdapter()})
        with self.assertRaises(PoolSyncError) as raised:
            await manager.add_manual_keys("http://127.0.0.1:8080", [{"key": "sk-x"}])
        self.assertIn("私有", str(raised.exception))


class PoolSyncManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tempdir.name, "sync.json")
        self.config = SimpleNamespace(
            key_pool_sync_state_file=self.state_file,
            key_pool_sync_default_adapter="sub2api",
            key_pool_sync_default_url="https://upstream.test",
            key_pool_sync_interval=0,
            key_pool_sync_secret="",
            provider="test-provider",
        )
        self.destination_check = patch(
            "retry_proxy.pool_sync._resolve_public_url_destination",
            new_callable=AsyncMock,
            side_effect=lambda url: (urlsplit(url), ("93.184.216.34",)),
        )
        self.destination_check.start()
        self.addCleanup(self.destination_check.stop)
        # 现有用例使用假域名，跳过 connect 的真实 DNS 反 SSRF 校验（专项用例单独覆盖）
        self.base_url_check = patch(
            "retry_proxy.pool_sync._validate_base_url_destination",
            new_callable=AsyncMock,
        )
        self.base_url_check.start()
        self.addCleanup(self.base_url_check.stop)

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

    async def test_encrypted_state_file_hides_credentials_and_survives_restart(self):
        secret_config = SimpleNamespace(**{**self.config.__dict__,
                                           "key_pool_sync_secret": "master-secret"})
        manager = PoolSyncManager(
            {}, secret_config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "plaintext-pw"},
        )
        source_id = status["sources"][0]["id"]

        with open(self.state_file, encoding="utf-8") as f:
            persisted = f.read()
        # Credentials must not appear in plaintext on disk
        self.assertNotIn("plaintext-pw", persisted)
        self.assertNotIn("refresh-1", persisted)
        self.assertNotIn("access-1", persisted)
        self.assertNotIn("sk-secret-one", persisted)
        self.assertIn("__encrypted__", persisted)

        # A second manager with the same secret can restore and re-sync
        restored_client = FakeClient()
        restored = PoolSyncManager(
            {}, secret_config, restored_client, {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        self.assertEqual(
            restored.pools["https://upstream.test"].entries[0].key,
            "sk-secret-one",
        )
        restored_status = await restored.sync_now(source_id)
        self.assertEqual(len(restored_status["sources"]), 1)

    async def test_encrypted_state_with_wrong_secret_clears_session(self):
        secret_config = SimpleNamespace(**{**self.config.__dict__,
                                           "key_pool_sync_secret": "right-secret"})
        manager = PoolSyncManager(
            {}, secret_config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "plaintext-pw"},
        )

        wrong_config = SimpleNamespace(**{**self.config.__dict__,
                                          "key_pool_sync_secret": "wrong-secret"})
        restored = PoolSyncManager(
            {}, wrong_config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        # Wrong key cannot decrypt; session cleared, source not considered connected
        source = next(iter(restored.sources.values()))
        self.assertEqual(source["session"], {})
        self.assertNotIn("https://upstream.test", restored.pools)

    async def test_group_model_cache_survives_restart(self):
        manager = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "secret"},
        )
        source_id = status["sources"][0]["id"]

        restored_client = FakeClient()
        restored = PoolSyncManager(
            {}, self.config, restored_client, {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        status = await restored.sync_now(source_id)

        model_calls = [
            call for call in restored_client.calls if call[1].endswith("/v1/models")
        ]
        self.assertEqual(model_calls, [])
        self.assertEqual(
            status["sources"][0]["keys"][0]["routing_capabilities"]["model_patterns"],
            ["gpt-5.4", "gpt-4.1"],
        )

    async def test_real_request_model_rejection_survives_sync_and_restart(self):
        manager = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "secret"},
        )
        source_id = status["sources"][0]["id"]

        recorded = await manager.mark_model_unsupported(
            "https://upstream.test", "2", "GPT-5.4",
        )

        self.assertTrue(recorded)
        self.assertFalse(await manager.mark_model_unsupported(
            "https://upstream.test", "2", "gpt-5.4",
        ))
        capabilities = manager.status()["sources"][0]["keys"][0]["routing_capabilities"]
        self.assertIn("gpt-5.4", capabilities["model_patterns"])
        self.assertEqual(capabilities["rejected_models"], ["gpt-5.4"])
        self.assertIsNone(manager.pools["https://upstream.test"].for_request(
            "gpt-5.4", "v1/chat/completions", "chat",
        ))
        # mark_model_unsupported throttles persistence; flush pending state
        # before simulating a restart (mirrors shutdown via stop()).
        manager._flush_state()

        restored = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        status = await restored.sync_now(source_id)

        capabilities = status["sources"][0]["keys"][0]["routing_capabilities"]
        self.assertEqual(capabilities["rejected_models"], ["gpt-5.4"])
        self.assertIsNone(restored.pools["https://upstream.test"].for_request(
            "gpt-5.4", "v1/chat/completions", "chat",
        ))

    async def test_mark_model_unsupported_throttles_persistence(self):
        manager = PoolSyncManager(
            {}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        await manager.connect(
            "sub2api", "https://upstream.test", "custom-provider",
            {"email": "user@example.com", "password": "secret"},
        )
        save_count = [0]
        original_save = manager._save_state

        def counting_save():
            save_count[0] += 1
            original_save()

        manager._save_state = counting_save
        # connect already saved once; reset counter to isolate throttle behavior
        save_count[0] = 0
        manager._last_state_save_at = time.monotonic()

        recorded = await manager.mark_model_unsupported(
            "https://upstream.test", "2", "gpt-5.4",
        )
        self.assertTrue(recorded)
        # Within the throttle window, _save_state should not have been called
        self.assertEqual(save_count[0], 0)
        self.assertTrue(manager._state_dirty)

        manager._flush_state()
        self.assertEqual(save_count[0], 1)
        self.assertFalse(manager._state_dirty)

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

    async def test_malformed_source_does_not_abort_other_restores(self):
        # Regression: a single malformed source (entry missing "key") used to
        # raise KeyError inside _activate and abort all source restores.
        state = {"version": 2, "sources": [
            {"id": "broken", "adapter": "sub2api", "base_url": "https://broken.test",
             "provider": "broken", "session": {},
             "entries": [{"label": "no-key", "sort": "0.1", "models": [], "paths": []}],
             "last_sync_at": "2026-07-17T00:00:00+00:00"},
            {"id": "good", "adapter": "sub2api", "base_url": "https://good.test",
             "provider": "good", "session": {"refresh_token": "r"},
             "entries": [{"key": "key-good", "label": "good", "sort": "0.1",
                          "models": [], "paths": []}]},
        ]}
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
        pools = {}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})

        manager.load_state()

        # The malformed source is skipped, but the good one still restores
        self.assertIn("good", manager.sources)
        self.assertNotIn("broken", manager.sources)
        self.assertIn("https://good.test", pools)

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
        for target in (pool, view):
            target._current = target.entries[-1]
            target._sticky_until = 10**12
            target._failover_floor = target._sort_value(target.entries[-1])
            target._balanced_group = target._group_key(target.entries[-1])
            target._probe_cursor_group = target._group_key(target.entries[-1])

        status = await manager.set_source_settings(
            source_id, "balanced", 4.5, "test-model", True, 0.75, 5,
        )

        source = status["sources"][0]
        self.assertEqual(source["strategy"], "balanced")
        self.assertEqual(source["target_ttft_s"], 4.5)
        self.assertEqual(source["external_retest_weight"], 0.75)
        self.assertEqual(source["external_ttft_prior_strength"], 5)
        self.assertTrue(source["session_affinity"])
        self.assertEqual(source["ttft_policy"]["confirmations"], 2)
        self.assertIn("scheduler_views", source)
        self.assertEqual(source["check_model"], "test-model")
        self.assertEqual(pool.strategy, "balanced")
        self.assertTrue(pool.session_affinity)
        self.assertEqual(pool.target_ttft_s, 4.5)
        self.assertEqual(pool.external_retest_weight, 0.75)
        self.assertEqual(pool.external_ttft_prior_strength, 5)
        self.assertEqual(view.external_retest_weight, 0.75)
        self.assertEqual(view.external_ttft_prior_strength, 5)
        self.assertEqual(pool._selection_count, 0)
        self.assertEqual(view._selection_count, 0)
        for target in (pool, view):
            self.assertIsNone(target._current)
            self.assertEqual(target._sticky_until, 0)
            self.assertIsNone(target._failover_floor)
            self.assertIsNone(target._balanced_group)
            self.assertIsNone(target._probe_cursor_group)
        with open(self.state_file, encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["sources"][0]["strategy"], "balanced")
        self.assertEqual(persisted["sources"][0]["external_retest_weight"], 0.75)
        self.assertEqual(persisted["sources"][0]["external_ttft_prior_strength"], 5)
        self.assertEqual(persisted["sources"][0]["check_model"], "test-model")
        self.assertTrue(persisted["sources"][0]["session_affinity"])
        restored_pools = {}
        restored = PoolSyncManager(
            restored_pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        self.assertEqual(
            restored_pools["https://upstream.test"].external_retest_weight, 0.75,
        )
        self.assertEqual(
            restored_pools["https://upstream.test"].external_ttft_prior_strength, 5,
        )

    async def test_source_strategy_rejects_non_finite_numbers(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})

        cases = [
            ({"target_ttft_s": float("nan")}, "首 Token 上限"),
            ({"external_retest_weight": float("inf")}, "外部复测权重"),
            ({"external_ttft_prior_strength": float("-inf")}, "外部参考强度"),
        ]
        for values, message in cases:
            with self.subTest(values=values), self.assertRaisesRegex(PoolSyncError, message):
                await manager.set_source_settings("missing", "balanced", **values)

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
        self.assertEqual(entry.ttft_samples, 0)
        self.assertIsNotNone(entry.probe_latency_s)

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
        first_batch_min = min(checks[str(index)]["response_s"] for index in range(2))
        self.assertLess(checks["2"]["response_s"], first_batch_min / 2)

    async def test_availability_network_probe_does_not_hold_manager_lock(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_probe(*_args, **_kwargs):
            started.set()
            await release.wait()
            return response({"choices": []}, 200)

        manager.client.post = blocked_probe
        check_task = asyncio.create_task(manager.check_availability(source_id, "test-model"))
        await started.wait()

        recorded = await asyncio.wait_for(
            manager.mark_model_unsupported(
                "https://upstream.test", "2", "other-model",
            ),
            timeout=0.2,
        )
        self.assertTrue(recorded)
        release.set()
        result = await check_task
        self.assertTrue(result["checks"][0]["available"])

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

    async def test_catalog_wraps_unexpected_adapter_error_as_pool_sync_error(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]

        stub = Sub2APIAdapter()
        stub.catalog = AsyncMock(side_effect=RuntimeError("boom inside adapter"))
        manager.adapters["sub2api"] = stub

        with self.assertRaises(PoolSyncError) as raised:
            await manager.catalog(source_id)

        # Regression: the original error used to be masked by an
        # UnboundLocalError because ``raise`` fell outside the except block.
        self.assertNotIsInstance(raised.exception.__cause__, UnboundLocalError)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertIn("读取分组失败", str(raised.exception))

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

    async def test_disabled_key_is_excluded_and_persists_across_sync_and_restart(self):
        pools = {"https://upstream.test": KeyPool([])}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]

        status = await manager.set_key_enabled(source_id, 11, False)

        self.assertEqual(pools["https://upstream.test"].entries, [])
        self.assertIsNone(pools["https://upstream.test"].for_request())
        self.assertFalse(status["sources"][0]["keys"][0]["enabled"])
        await manager.sync_now(source_id)
        self.assertEqual(pools["https://upstream.test"].entries, [])

        restored_pools = {}
        restored = PoolSyncManager(
            restored_pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        self.assertEqual(restored_pools["https://upstream.test"].entries, [])
        self.assertFalse(restored.status()["sources"][0]["keys"][0]["enabled"])

        status = await restored.set_key_enabled(source_id, 11, True)
        self.assertEqual(
            [entry.key for entry in restored_pools["https://upstream.test"].entries],
            ["sk-secret-one"],
        )
        self.assertTrue(status["sources"][0]["keys"][0]["enabled"])

    async def test_set_key_enabled_validates_key_and_boolean(self):
        manager = PoolSyncManager({}, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()})
        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]

        with self.assertRaisesRegex(PoolSyncError, "enabled 必须是布尔值"):
            await manager.set_key_enabled(source_id, 11, "false")
        with self.assertRaisesRegex(PoolSyncError, "Key 不存在"):
            await manager.set_key_enabled(source_id, 999, False)

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

    async def test_external_data_source_maps_groups_and_decays_ttft_prior(self):
        client = FakeClient()
        client.created.append(3)
        original_get = client.get
        external_url = "https://metrics.test/group-latency"

        async def get_with_external(url, params=None, headers=None, timeout=None):
            if url == external_url:
                self.assertEqual(params, {"limit": "50", "scope": "public"})
                return response({"payload": {"groups": [
                    {"remote_id": "slow", "label": "Slow", "latency_ms": 8000,
                     "count": 50, "time": "2026-07-25T18:41:34Z"},
                    {"remote_id": "fast", "label": "Fast", "latency_ms": 1000,
                     "count": 50, "time": "2026-07-25T18:41:34Z"},
                ]}})
            return await original_get(url, params, headers, timeout)

        client.get = get_with_external
        pools = {}
        manager = PoolSyncManager(
            pools, self.config, client, {"sub2api": Sub2APIAdapter()},
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "test",
            {"email": "user@example.com", "password": "secret"},
        )
        source_id = status["sources"][0]["id"]
        transform = {
            "items_path": "payload.groups", "id_path": "remote_id",
            "name_path": "label", "platform_path": "", "rate_path": "",
            "ttft_path": "latency_ms", "ttft_unit": "ms",
            "samples_path": "count", "timestamp_path": "time",
        }

        await manager.set_experience_source(
            source_id, external_url, transform=transform,
            query_params={"limit": 50, "scope": "public"},
        )
        await manager.set_experience_mapping(source_id, {"2": "slow", "3": "fast"})
        await manager.set_source_settings(source_id, "ttft")

        pool = pools["https://upstream.test"]
        self.assertEqual(pool.pick().group_id, "3")
        fast = next(entry for entry in pool.entries if entry.group_id == "3")
        pool.record_ttft(fast, 12.0)
        self.assertEqual(pool.pick().group_id, "3")
        for _ in range(3):
            pool.record_ttft(fast, 12.0)
        self.assertEqual(pool.pick().group_id, "2")

        restored_pools = {}
        restored = PoolSyncManager(
            restored_pools, self.config, FakeClient(), {"sub2api": Sub2APIAdapter()},
        )
        restored.load_state()
        restored_pool = restored_pools["https://upstream.test"]
        self.assertEqual(restored_pool.prior_metrics["3"]["ttft"], 1.0)
        self.assertEqual(restored_pool.strategy, "ttft")

        await manager.set_source_settings(source_id, "cost")
        self.assertEqual(pool.pick().group_id, "2")
        public = manager.status()["sources"][0]
        self.assertEqual(public["experience"]["mappings"], {"2": "slow", "3": "fast"})
        self.assertIn("external_group_id", pool.prior_metrics["2"])

        changed_transform = {**transform, "id_path": "label"}
        status = await manager.set_experience_source(
            source_id, external_url, transform=changed_transform,
            query_params={"limit": 50, "scope": "public"},
        )
        self.assertEqual(status["sources"][0]["experience"]["mappings"], {})

        await manager.set_experience_source(source_id, "")
        self.assertEqual(pool.prior_metrics, {})
        with open(self.state_file, encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["sources"][0]["experience_source"], {})

    async def test_external_refresh_failure_does_not_fail_normal_pool_sync(self):
        client = FakeClient()
        original_get = client.get
        external_url = "https://metrics.test/groups"
        fail = False

        async def get_with_external(url, params=None, headers=None, timeout=None):
            if url == external_url:
                if fail:
                    return response({"error": "down"}, 503)
                return response({"data": {"items": [{
                    "group_id": 10, "code": "Fast", "avg_ttft_ms": 500,
                    "sample_count": 10, "last_sample_at": "2026-07-25T18:41:34Z",
                }]}})
            return await original_get(url, params, headers, timeout)

        client.get = get_with_external
        manager = PoolSyncManager(
            {}, self.config, client, {"sub2api": Sub2APIAdapter()},
        )
        status = await manager.connect(
            "sub2api", "https://upstream.test", "test",
            {"email": "user@example.com", "password": "secret"},
        )
        source_id = status["sources"][0]["id"]
        await manager.set_experience_source(source_id, external_url, transform={
            "items_path": "data.items", "id_path": "group_id",
            "ttft_path": "avg_ttft_ms", "ttft_unit": "ms",
            "name_path": "code", "platform_path": "", "rate_path": "",
            "samples_path": "sample_count", "timestamp_path": "last_sample_at",
        }, query_params={})
        fail = True

        status = await manager.sync_now(source_id)

        source = status["sources"][0]
        self.assertEqual(source["key_count"], 1)
        self.assertIn("HTTP 503", source["experience"]["last_error"])
        self.assertEqual(len(source["experience"]["items"]), 1)

    def test_single_existing_pool_is_used_as_generic_default_url(self):
        self.config.key_pool_sync_default_url = "https://default-without-pool.test"
        manager = PoolSyncManager(
            {"https://configured-pool.test": KeyPool(["key"])}, self.config,
            FakeClient(), {"sub2api": Sub2APIAdapter()},
        )

        self.assertEqual(manager.status()["defaults"]["base_url"], "https://configured-pool.test")


class ManualAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tempdir.name, "sync.json")
        self.config = SimpleNamespace(
            key_pool_sync_state_file=self.state_file,
            key_pool_sync_default_adapter="manual",
            key_pool_sync_default_url="https://manual.test",
            key_pool_sync_interval=0,
            key_pool_sync_secret="manual-test-secret",
            provider="manual-provider",
            extra_upstreams="",
            upstream_url="https://default.test",
        )
        # 现有用例使用假域名，跳过真实 DNS 反 SSRF 校验（专项用例单独覆盖）
        self.base_url_check = patch(
            "retry_proxy.pool_sync._validate_base_url_destination",
            new_callable=AsyncMock,
        )
        self.base_url_check.start()
        self.addCleanup(self.base_url_check.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    async def test_add_manual_keys_creates_source_and_pool(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "Key One", "sort": "1"}],
        )

        self.assertIn("https://manual.test", pools)
        self.assertEqual(len(pools["https://manual.test"].entries), 1)
        self.assertEqual(pools["https://manual.test"].entries[0].key, "sk-key-1")
        self.assertEqual(pools["https://manual.test"].entries[0].label, "Key One")
        self.assertEqual(status["sources"][0]["adapter"], "manual")
        self.assertEqual(status["sources"][0]["key_count"], 1)

    async def test_add_manual_keys_appends_to_existing(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "First"}],
        )
        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-2", "label": "Second"}],
        )

        self.assertEqual(len(pools["https://manual.test"].entries), 2)
        self.assertEqual(status["sources"][0]["key_count"], 2)

    async def test_add_manual_keys_skips_duplicate(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1"}],
        )
        # Adding the same key again should skip
        with self.assertRaises(PoolSyncError):
            await manager.add_manual_keys(
                "https://manual.test",
                [{"key": "sk-key-1"}],
            )
        # Pool still has just 1 entry
        self.assertEqual(len(pools["https://manual.test"].entries), 1)

    async def test_add_manual_keys_deduplicates_within_one_batch(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "First"},
             {"key": "sk-key-1", "label": "Duplicate"}],
        )

        source = next(iter(manager.sources.values()))
        self.assertEqual(len(source["entries"]), 1)
        self.assertEqual(status["sources"][0]["key_count"], 1)
        self.assertEqual(len(pools["https://manual.test"].entries), 1)

    async def test_add_manual_keys_rejects_malformed_items_and_fields(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        manager = PoolSyncManager({}, self.config, None, {"manual": ManualAdapter()})

        with self.assertRaisesRegex(PoolSyncError, r"keys\[0\] 必须是对象"):
            await manager.add_manual_keys("https://manual.test", ["not-an-object"])
        with self.assertRaisesRegex(PoolSyncError, r"keys\[0\]\.sort 必须是字符串"):
            await manager.add_manual_keys(
                "https://manual.test", [{"key": "sk-key-1", "sort": 1}],
            )

        self.assertEqual(manager.sources, {})

    async def test_add_manual_keys_with_models_and_paths(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "group_name": "shared",
              "models": "gpt-4*;claude*", "paths": "v1/chat/*"}],
        )

        entry = pools["https://manual.test"].entries[0]
        self.assertEqual(entry.group_id, "shared")
        self.assertEqual(entry.models, ("gpt-4*", "claude*"))
        self.assertEqual(entry.paths, ("v1/chat/*",))

    async def test_route_conflict_rejects_add_without_partial_source(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        registry = RouteRegistry(self.config)
        registry.register("existing", "/shared", "https://existing.test", "existing")
        pools = {}
        manager = PoolSyncManager(
            pools, self.config, None, {"manual": ManualAdapter()}, registry,
        )

        with self.assertRaisesRegex(PoolSyncError, "已被其他号池连接使用"):
            await manager.add_manual_keys(
                "https://manual.test", [{"key": "sk-key-1"}],
                route_prefix="/shared",
            )

        self.assertEqual(manager.sources, {})
        self.assertNotIn("https://manual.test", pools)
        self.assertEqual(
            registry.match("shared/v1/chat")[:2],
            ("https://existing.test", "existing"),
        )

    async def test_manual_source_without_reachable_route_is_rejected(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        registry = RouteRegistry(self.config)
        pools = {}
        manager = PoolSyncManager(
            pools, self.config, None, {"manual": ManualAdapter()}, registry,
        )

        with self.assertRaisesRegex(PoolSyncError, "填写代理前缀"):
            await manager.add_manual_keys(
                "https://unrouted.test", [{"key": "sk-key-1"}],
            )

        self.assertEqual(manager.sources, {})
        self.assertNotIn("https://unrouted.test", pools)

    async def test_manual_source_for_default_upstream_can_omit_route_prefix(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        registry = RouteRegistry(self.config)
        pools = {}
        manager = PoolSyncManager(
            pools, self.config, None, {"manual": ManualAdapter()}, registry,
        )

        await manager.add_manual_keys(
            self.config.upstream_url, [{"key": "sk-key-1"}],
        )

        self.assertIn(self.config.upstream_url, pools)
        self.assertEqual(
            registry.match("v1/chat/completions")[0], self.config.upstream_url,
        )

    async def test_existing_manual_source_rejects_route_changes(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        registry = RouteRegistry(self.config)
        manager = PoolSyncManager(
            {}, self.config, None, {"manual": ManualAdapter()}, registry,
        )
        await manager.add_manual_keys(
            "https://manual.test", [{"key": "sk-key-1"}],
            route_prefix="/manual",
        )

        with self.assertRaisesRegex(PoolSyncError, "其他代理前缀"):
            await manager.add_manual_keys(
                "https://manual.test", [{"key": "sk-key-2"}],
                route_prefix="/changed",
            )
        with self.assertRaisesRegex(PoolSyncError, "其他代理前缀"):
            await manager.add_manual_keys(
                "https://manual.test", [{"key": "sk-key-2"}],
                route_prefix="",
            )

        source = next(iter(manager.sources.values()))
        self.assertEqual(len(source["entries"]), 1)
        self.assertEqual(registry.match("manual/v1/chat")[0], "https://manual.test")

    async def test_remove_manual_keys(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1"}, {"key": "sk-key-2"}],
        )
        source_id = status["sources"][0]["id"]
        source_key_id = status["sources"][0]["keys"][0]["source_key_id"]

        status = await manager.remove_manual_keys(source_id, [source_key_id])
        self.assertEqual(len(pools["https://manual.test"].entries), 1)
        self.assertEqual(pools["https://manual.test"].entries[0].key, "sk-key-2")

    async def test_remove_all_manual_keys_deletes_source(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1"}],
        )
        source_id = status["sources"][0]["id"]
        source_key_id = status["sources"][0]["keys"][0]["source_key_id"]

        await manager.remove_manual_keys(source_id, [source_key_id])
        self.assertNotIn("https://manual.test", pools)
        self.assertEqual(len(manager.sources), 0)

    async def test_update_manual_key(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "Original"}],
        )
        source_id = status["sources"][0]["id"]
        source_key_id = status["sources"][0]["keys"][0]["source_key_id"]

        await manager.update_manual_key(source_id, source_key_id, {"label": "Updated", "sort": "2"})
        self.assertEqual(pools["https://manual.test"].entries[0].label, "Updated")
        self.assertEqual(pools["https://manual.test"].entries[0].sort, "2")

    async def test_update_manual_key_group_and_patterns(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "Original"}],
        )
        source_id = status["sources"][0]["id"]
        source_key_id = status["sources"][0]["keys"][0]["source_key_id"]

        await manager.update_manual_key(source_id, source_key_id, {
            "group_id": "vip", "group_name": "vip",
            "models": "gpt-4*;claude*", "paths": "v1/images/*;v1/chat/*",
        })
        entry = pools["https://manual.test"].entries[0]
        self.assertEqual(entry.group_name, "vip")
        self.assertEqual(entry.group_id, "vip")
        self.assertEqual(entry.models, ("gpt-4*", "claude*"))
        self.assertEqual(entry.paths, ("v1/images/*", "v1/chat/*"))
        # 未传字段保持不变（部分更新语义）
        self.assertEqual(entry.label, "Original")

    async def test_manual_source_persistence(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "Persisted"}],
        )
        source_id = status["sources"][0]["id"]

        with open(self.state_file, encoding="utf-8") as f:
            persisted_text = f.read()
        self.assertNotIn("sk-key-1", persisted_text)
        self.assertIn("__encrypted__", persisted_text)

        # Simulate restart
        restored_pools = {}
        restored = PoolSyncManager(
            restored_pools, self.config, None, {"manual": ManualAdapter()},
        )
        restored.load_state()

        self.assertIn(source_id, restored.sources)
        self.assertEqual(len(restored_pools["https://manual.test"].entries), 1)
        self.assertEqual(restored_pools["https://manual.test"].entries[0].key, "sk-key-1")

    async def test_manual_adapter_cannot_use_generic_connect(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        manager = PoolSyncManager({}, self.config, None, {"manual": ManualAdapter()})

        with self.assertRaisesRegex(PoolSyncError, "手动添加 Key"):
            await manager.connect(
                "manual", "https://manual.test", "manual-provider", {}, "/manual",
            )

    async def test_cannot_add_manual_to_online_sync_url(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"manual": ManualAdapter(), "sub2api": Sub2APIAdapter()})

        # First connect via sub2api
        await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })

        # Then try to add manual keys to the same URL
        with self.assertRaises(PoolSyncError) as raised:
            await manager.add_manual_keys(
                "https://upstream.test",
                [{"key": "sk-manual-1"}],
            )
        self.assertIn("在线同步连接接管", str(raised.exception))

    async def test_cannot_remove_from_non_manual_source(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, FakeClient(), {"manual": ManualAdapter(), "sub2api": Sub2APIAdapter()})

        status = await manager.connect("sub2api", "https://upstream.test", "test", {
            "email": "user@example.com", "password": "secret",
        })
        source_id = status["sources"][0]["id"]

        with self.assertRaises(PoolSyncError) as raised:
            await manager.remove_manual_keys(source_id, ["any-key"])
        self.assertIn("手动管理", str(raised.exception))

    async def test_manual_source_not_auto_synced(self):
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1"}],
        )

        self.assertFalse(manager._has_connected_sources())

    async def test_reset_group_accepts_source_key_id_for_manual_key_without_group(self):
        """手动 Key 未填分组时前端传 source_key_id，reset_group 仍应解除熔断。

        回归:前端 data-reset-group 在 group_id 为空时回退到 source_key_id，
        而后端按 (entry.group_id or entry.key) 匹配，对未分组手动 Key 会因
        source_key_id(哈希) 与 entry.key(原文) 不一致而报“分组不存在或尚未加载”。
        """
        from retry_proxy.sync_adapters.manual import ManualAdapter
        pools = {}
        manager = PoolSyncManager(pools, self.config, None, {"manual": ManualAdapter()})

        status = await manager.add_manual_keys(
            "https://manual.test",
            [{"key": "sk-key-1", "label": "Key One"}],
        )
        source_id = status["sources"][0]["id"]
        visible = status["sources"][0]["keys"][0]
        # 未填分组时前端回退到 source_key_id 作为 group_id 传给 reset-group
        self.assertEqual(visible["group_id"], "")
        group_id = visible["source_key_id"]

        pool = pools["https://manual.test"]
        entry = pool.entries[0]
        pool.mark_cooldown(entry, 1800, failure_kind="auth", status=403)
        self.assertTrue(entry.cooldown_until > 0)

        with self.assertLogs("forward", level="INFO") as captured:
            status = await manager.reset_group(source_id, group_id)

        self.assertEqual(entry.cooldown_until, 0)
        refreshed = next(item for item in status["sources"][0]["keys"]
                         if item["source_key_id"] == group_id)
        self.assertFalse(refreshed["cooled"])
        # 回退解析后 group_key 为原始 Key，日志不得泄漏明文
        log_text = "\n".join(captured.output)
        self.assertNotIn("sk-key-1", log_text)
        self.assertIn("已手动解除熔断", log_text)


if __name__ == "__main__":
    unittest.main()
