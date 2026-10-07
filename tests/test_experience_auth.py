import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from retry_proxy.experience_data import _experience_timestamp
from retry_proxy.pool_sync import PoolSyncManager
from retry_proxy.sync_adapters import PoolSyncError
from retry_proxy.sync_adapters.new_api import NewAPIAdapter


class ExperienceAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = NewAPIAdapter()
        self.manager = PoolSyncManager(
            {}, SimpleNamespace(key_pool_experience_timeout=75), object(),
            {"newapi": self.adapter},
        )
        self.source = {
            "adapter": "newapi", "base_url": "https://upstream.test",
            "session": {"access_token": "test-access", "user_id": 7},
        }
        self.config = self.manager._normalize_experience_source(
            "https://upstream.test/api/model_probe/overview",
            query_params={"hours": 24, "lang": "zh"}, auth_mode="source_session",
            transform={"items_path": "data.targets", "id_path": "group",
                       "ttft_path": "math.latest.first_token_ms",
                       "timestamp_path": "math.latest.ts",
                       "detection_path": "math.latest.status",
                       "detection_map": {"pass": "智力正常"}},
        )
        self.payload = {"success": True, "data": {"targets": [{
            "group": "test-group", "math": {"latest": {
                "first_token_ms": 22043, "ts": 1791259200, "status": "pass",
            }},
        }]}}

    async def test_authenticated_fetch_preserves_envelope_and_parameters(self):
        with patch("retry_proxy.pool_sync._get_pinned_public_url", new_callable=AsyncMock,
                   return_value=httpx.Response(200, json=self.payload)) as fetch:
            items = await self.manager._fetch_experience_items(self.config, self.source)
        kwargs = fetch.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-access")
        self.assertEqual(kwargs["params"], {"hours": "24", "lang": "zh"})
        self.assertEqual(kwargs["timeout"], 75)
        self.assertEqual(items[0]["ttft"], 22.043)
        self.assertEqual(items[0]["last_ts"], 1791259200)
        self.assertEqual(items[0]["detection_label"], "智力正常")
        self.assertNotIn("test-access", str(items))

    async def test_expired_session_refreshes_and_retries_pinned_fetch(self):
        self.source["session"]["cookies"] = {"new_api_refresh": "test-refresh"}
        refreshed = {"access_token": "new-test-access", "cookies": {}}
        with patch.object(self.adapter, "_refresh", new_callable=AsyncMock,
                          return_value=refreshed) as refresh, patch(
                "retry_proxy.pool_sync._get_pinned_public_url", new_callable=AsyncMock,
                side_effect=[httpx.Response(401, json={"success": False}),
                             httpx.Response(200, json=self.payload)]) as fetch:
            await self.manager._fetch_experience_items(self.config, self.source)
        refresh.assert_awaited_once()
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(fetch.call_args.kwargs["headers"]["Authorization"],
                         "Bearer new-test-access")
        self.assertEqual(self.source["session"]["access_token"], "new-test-access")

    async def test_cross_origin_and_http_rejected_before_sending_credentials(self):
        for url in ("https://other.test/api", "https://upstream.test:444/api",
                    "http://upstream.test/api", "https://user@upstream.test/api"):
            with self.subTest(url=url), patch(
                    "retry_proxy.pool_sync._get_pinned_public_url", new_callable=AsyncMock) as fetch:
                with self.assertRaises(PoolSyncError):
                    await self.manager._fetch_experience_items({**self.config, "url": url}, self.source)
                fetch.assert_not_awaited()

    async def test_redirect_rejected_and_cancellation_propagates(self):
        with patch("retry_proxy.pool_sync._get_pinned_public_url", new_callable=AsyncMock,
                   return_value=httpx.Response(302, headers={"location": "https://other.test"})) as fetch:
            with self.assertRaisesRegex(PoolSyncError, "重定向"):
                await self.manager._fetch_experience_items(self.config, self.source)
            self.assertEqual(fetch.await_count, 1)
        with patch("retry_proxy.pool_sync._get_pinned_public_url", new_callable=AsyncMock,
                   side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._fetch_experience_items(self.config, self.source)

    def test_only_pass_and_unknown_are_enabled_for_authenticated_source(self):
        states = ["pass", "", "unknown", "fail", "error", "running", "pending", "other", "ok"]
        source = {"experience_source": self.config,
                  "experience_items": [{"id": str(i), "detection_status": state}
                                       for i, state in enumerate(states)],
                  "experience_mappings": {str(i): str(i) for i in range(len(states))}}
        self.assertEqual(self.manager._detection_disabled_group_ids(source),
                         {str(i) for i in range(3, len(states))})

    def test_timestamp_accepts_unix_seconds_and_iso(self):
        self.assertEqual(_experience_timestamp("1791259200"), 1791259200)
        self.assertEqual(_experience_timestamp("1970-01-01T00:00:01Z"), 1)
        self.assertEqual(_experience_timestamp("NaN"), 0)
