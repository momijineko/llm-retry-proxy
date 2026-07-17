import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from retry_proxy.key_pool import KeyEntry, KeyPool
from retry_proxy.retry import (RetryProxy, _key_available_for_status, _key_failure_policy,
                               _mark_key_failure, _pick_key, _select_key_failure_status)


class KeyPoolStickyTests(unittest.TestCase):
    def test_auth_failures_are_not_recorded_as_available_keys(self):
        self.assertFalse(_key_available_for_status(401))
        self.assertFalse(_key_available_for_status(403))
        self.assertTrue(_key_available_for_status(400))
        self.assertTrue(_key_available_for_status(200))

    def test_failure_statuses_use_separate_cooldown_bases(self):
        config = SimpleNamespace(key_cooldown=10, key_cooldown_5xx=30, key_cooldown_429=60,
                                 key_cooldown_auth=1800)

        self.assertEqual(_key_failure_policy(config, 503), ("upstream", 30))
        self.assertEqual(_key_failure_policy(config, 429), ("rate_limit", 60))
        self.assertEqual(_key_failure_policy(config, 401), ("auth", 1800))
        self.assertEqual(_key_failure_policy(config, 403), ("auth", 1800))
        self.assertEqual(_key_failure_policy(config, 0), ("transport", 30))
        self.assertEqual(_select_key_failure_status([503, 429, 401]), 401)
        self.assertEqual(_select_key_failure_status([503, 429]), 429)
        self.assertEqual(_select_key_failure_status([0, 503]), 503)
        self.assertEqual(_select_key_failure_status([0]), 0)

    def test_more_severe_inflight_failure_upgrades_circuit_metadata(self):
        pool = KeyPool([("key", "key")])
        entry = pool.entries[0]
        config = SimpleNamespace(key_cooldown=30, key_cooldown_5xx=30, key_cooldown_429=60,
                                 key_cooldown_auth=1800, key_cooldown_max=3600,
                                 key_cooldown_backoff=True)

        with patch("retry_proxy.key_pool.time.time", return_value=100):
            _mark_key_failure(pool, entry, config, 503)
        with patch("retry_proxy.key_pool.time.time", return_value=101):
            _mark_key_failure(pool, entry, config, 401)

        self.assertEqual(entry.cooldown_until, 1901)
        self.assertEqual(entry.consecutive_failures, 1)
        self.assertEqual(entry.last_failure_kind, "auth")
        self.assertEqual(entry.last_failure_status, 401)

        pool.mark_success(entry)
        with patch("retry_proxy.key_pool.time.time", return_value=2000):
            _mark_key_failure(pool, entry, config, 429, ra_wait=3600)
        with patch("retry_proxy.key_pool.time.time", return_value=2001):
            _mark_key_failure(pool, entry, config, 403)

        self.assertEqual(entry.cooldown_until, 5600)
        self.assertEqual(entry.last_failure_kind, "auth")
        self.assertEqual(entry.last_failure_status, 403)

    def test_repeated_failures_back_off_and_success_resets_state(self):
        pool = KeyPool([("key", "key")])
        entry = pool.entries[0]
        config = SimpleNamespace(key_cooldown=30, key_cooldown_5xx=30, key_cooldown_429=60,
                                 key_cooldown_auth=1800, key_cooldown_max=100,
                                 key_cooldown_backoff=True)

        with patch("retry_proxy.key_pool.time.time", return_value=100):
            _mark_key_failure(pool, entry, config, 503)
        self.assertEqual(entry.cooldown_until, 130)
        self.assertEqual(entry.consecutive_failures, 1)

        with patch("retry_proxy.key_pool.time.time", return_value=140):
            _mark_key_failure(pool, entry, config, 503)
        self.assertEqual(entry.cooldown_until, 200)
        self.assertEqual(entry.consecutive_failures, 2)

        with patch("retry_proxy.key_pool.time.time", return_value=210):
            _mark_key_failure(pool, entry, config, 503)
        self.assertEqual(entry.cooldown_until, 310)
        self.assertEqual(entry.consecutive_failures, 3)
        self.assertEqual(entry.last_cooldown_s, 100)

        pool.mark_success(entry)
        self.assertEqual(entry.cooldown_until, 0)
        self.assertEqual(entry.consecutive_failures, 0)

        with patch("retry_proxy.key_pool.time.time", return_value=400):
            _mark_key_failure(pool, entry, config, 429, ra_wait=150)
        self.assertEqual(entry.cooldown_until, 550)
        self.assertEqual(entry.consecutive_failures, 1)
        self.assertEqual(entry.last_failure_kind, "rate_limit")

    def test_duplicate_labels_get_unique_non_secret_ids(self):
        pool = KeyPool([("sk-secret-one", "shared"), ("sk-secret-two", "shared")])

        ids = [entry.key_id for entry in pool.entries]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(key_id.startswith("shared#") for key_id in ids))
        self.assertTrue(all("secret" not in key_id for key_id in ids))

    def test_duplicate_raw_keys_are_removed(self):
        pool = KeyPool([("same-key", "first"), ("same-key", "second")])

        self.assertEqual(len(pool.entries), 1)
        self.assertEqual(pool.entries[0].label, "first")

    def test_sticky_window_renews_until_idle_timeout(self):
        pool = KeyPool([("cheap", "cheap"), ("expensive", "expensive")])
        pool._current = pool.entries[1]
        pool._sticky_until = 100
        fake_settings = SimpleNamespace(key_sticky=120)

        with patch("retry_proxy.key_pool.settings", fake_settings):
            with patch("retry_proxy.key_pool.time.time", return_value=50):
                self.assertEqual(pool.pick().key_id, "expensive")
                self.assertEqual(pool._sticky_until, 170)
            with patch("retry_proxy.key_pool.time.time", return_value=160):
                self.assertEqual(pool.pick().key_id, "expensive")
                self.assertEqual(pool._sticky_until, 280)
            with patch("retry_proxy.key_pool.time.time", return_value=281):
                self.assertEqual(pool.pick().key_id, "cheap")
                self.assertEqual(pool._sticky_until, 401)

    def test_model_and_path_rules_create_isolated_pools(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("normal-1", "normal-1"),
            KeyEntry("normal-2", "normal-2"),
            KeyEntry("image-1", "image-1", models=("gpt-image-*",), paths=("images/*",)),
            KeyEntry("image-2", "image-2", models=("gpt-image-*",), paths=("images/*",)),
        ]

        normal = pool.for_request("gpt-text", "chat/completions")
        image_by_model = pool.for_request("gpt-image-1", "responses")
        image_by_path = pool.for_request("", "/images/generations")

        self.assertEqual([entry.key_id for entry in normal.entries], ["normal-1", "normal-2"])
        self.assertEqual([entry.key_id for entry in image_by_model.entries], ["image-1", "image-2"])
        self.assertIs(image_by_model, image_by_path)
        self.assertIsNot(normal, image_by_model)

        image_by_model._current = image_by_model.entries[1]
        image_by_model._sticky_until = 999
        self.assertIsNone(normal._current)

    def test_specific_pool_never_falls_back_to_default_entries(self):
        pool = KeyPool([])
        normal = KeyEntry("normal", "normal")
        image = KeyEntry("image", "image", models=("gpt-image-*",))
        pool.entries = [normal, image]
        image.cooldown_until = 999
        scoped = pool.for_request("gpt-image-1", "responses")
        with patch("retry_proxy.key_pool.time.time", return_value=100):
            self.assertEqual(scoped.pick().key_id, "image")


class KeyPoolCooldownWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_waits_instead_of_bypassing_an_open_circuit(self):
        pool = KeyPool([("key", "key")])
        entry = pool.entries[0]
        entry.cooldown_until = 130

        def finish_cooldown(_seconds):
            entry.cooldown_until = 0

        with patch("retry_proxy.key_pool.time.time", return_value=100), \
                patch("retry_proxy.retry.time.time", return_value=100), \
                patch("retry_proxy.retry.asyncio.sleep", new_callable=AsyncMock,
                      side_effect=finish_cooldown) as sleep:
            selected = await _pick_key(pool)

        self.assertIs(selected, entry)
        sleep.assert_awaited_once_with(30)

    async def test_race_exhaustion_still_opens_key_circuit(self):
        pool = KeyPool([("key", "key")])
        config = SimpleNamespace(
            hedge_mode="race", max_concurrent=1, max_retries=1,
            key_cooldown=30, key_cooldown_5xx=30, key_cooldown_429=60,
            key_cooldown_auth=1800, key_cooldown_max=3600, key_cooldown_backoff=True,
        )
        response = SimpleNamespace(status_code=503, headers={})
        response.aread = AsyncMock(return_value=b"")
        response.aclose = AsyncMock()
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(return_value=response)

        result = await proxy.request("POST", "https://upstream.test", {}, b"{}",
                                     "v1/chat", "test", "model", pool)

        self.assertIsNone(result.response)
        self.assertTrue(pool.entries[0].cooldown_until > 0)
        self.assertEqual(pool.entries[0].last_failure_status, 503)


if __name__ == "__main__":
    unittest.main()
