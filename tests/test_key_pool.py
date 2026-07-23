import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from retry_proxy.api import (classify_endpoint, classify_model_scope,
                             is_model_rejection_response, parse_request_model)
from retry_proxy.key_pool import KeyEntry, KeyPool, headers_with_key, replace_key_pool
from retry_proxy.retry import (KeyPoolWaitTimeout, RetryProxy, _key_available_for_status,
                               _key_failure_policy, _mark_key_failure, _pick_key,
                               _select_key_failure_status)


class KeyPoolStickyTests(unittest.TestCase):
    def test_ttft_strategy_selects_fastest_group(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("fast", "fast", sort="0.10", group_id="fast"),
        ]
        pool.finalize_entries()
        pool.strategy = "ttft"
        pool.record_ttft(pool.entries[0], 8.0)
        pool.record_ttft(pool.entries[1], 1.0)

        self.assertEqual(pool.pick().group_id, "fast")

    def test_balanced_strategy_upgrades_after_two_slow_samples(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("slow-cheap", "slow-cheap", sort="0.01", group_id="slow"),
            KeyEntry("good", "good", sort="0.03", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool.target_ttft_s = 5

        first = pool.pick()
        pool.record_ttft(first, 5.6)
        self.assertEqual(pool.pick().group_id, "slow")
        pool.record_ttft(first, 5.7)
        pool._sticky_until = 0
        self.assertEqual(pool.pick().group_id, "good")

    def test_balanced_strategy_keeps_group_inside_hysteresis_band(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("premium", "premium", sort="0.20", group_id="premium"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool.target_ttft_s = 5

        selected = pool.pick()
        pool.record_ttft(selected, 5.2)
        pool.record_ttft(selected, 5.3)

        self.assertEqual(selected.group_id, "cheap")
        self.assertEqual(pool.pick().group_id, "cheap")

    def test_balanced_retests_and_recovers_cheaper_group(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool.target_ttft_s = 5
        cheap = pool.pick()
        pool.record_ttft(cheap, 6.0)
        pool.record_ttft(cheap, 6.0)
        pool._sticky_until = 0
        self.assertEqual(pool.pick().group_id, "good")

        cheap_metric = pool._metric("cheap")
        cheap_metric["last_ts"] -= 301
        cheap_metric["next_probe_at"] = 0
        pool._sticky_until = 0
        probe = pool.pick()
        self.assertEqual(probe.group_id, "cheap")
        pool.record_ttft(probe, 4.4)
        pool._sticky_until = 0
        self.assertEqual(pool.pick().group_id, "good")

        cheap_metric["next_probe_at"] = 0
        pool._sticky_until = 0
        second_probe = pool.pick()
        pool.record_ttft(second_probe, 4.3)
        pool._sticky_until = 0
        self.assertEqual(pool.pick().group_id, "cheap")

    def test_balanced_allows_only_one_inflight_cheaper_probe(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool._balanced_group = "good"

        self.assertEqual(pool.pick().group_id, "cheap")
        pool._sticky_until = 0
        self.assertEqual(pool.pick().group_id, "good")

    def test_balanced_sticky_window_defers_cheaper_probe_until_expiry(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool._balanced_group = "good"
        pool._current = pool.entries[1]
        pool._sticky_until = 200
        fake_settings = SimpleNamespace(key_sticky=120, key_ttft_stale_after=300,
                                        key_ttft_retest_interval=60)

        with patch("retry_proxy.key_pool.settings", fake_settings):
            with patch("retry_proxy.key_pool.time.time", return_value=100):
                self.assertEqual(pool.pick().group_id, "good")
                self.assertEqual(pool._metric("cheap")["probe_reserved_until"], 0)
            with patch("retry_proxy.key_pool.time.time", return_value=221):
                self.assertEqual(pool.pick().group_id, "cheap")
                self.assertGreater(pool._metric("cheap")["probe_reserved_until"], 221)

    def test_balanced_sticky_window_does_not_block_failed_key_failover(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool._balanced_group = "cheap"
        pool._current = pool.entries[0]
        pool._sticky_until = 200
        pool.entries[0].cooldown_until = 150
        fake_settings = SimpleNamespace(key_sticky=120, key_ttft_stale_after=300,
                                        key_ttft_retest_interval=60)

        with patch("retry_proxy.key_pool.settings", fake_settings), \
                patch("retry_proxy.key_pool.time.time", return_value=100):
            self.assertEqual(pool.pick().group_id, "good")

    def test_failed_candidate_does_not_create_or_renew_sticky_window(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        fake_settings = SimpleNamespace(key_sticky=120, key_ttft_stale_after=300,
                                        key_ttft_retest_interval=60)

        with patch("retry_proxy.key_pool.settings", fake_settings), \
                patch("retry_proxy.key_pool.time.time", return_value=100):
            candidate = pool.pick()
            pool.mark_cooldown(candidate, 30, status=503)

        self.assertIsNone(pool._current)
        self.assertEqual(pool._sticky_until, 0.0)

    def test_successful_candidate_starts_and_renews_sticky_window(self):
        pool = KeyPool([("key", "key")])
        entry = pool.entries[0]
        fake_settings = SimpleNamespace(key_sticky=120)

        with patch("retry_proxy.key_pool.settings", fake_settings), \
                patch("retry_proxy.key_pool.time.time", return_value=100):
            pool.mark_success(entry)

        self.assertIs(pool._current, entry)
        self.assertEqual(pool._sticky_until, 220)

    def test_failed_cheaper_probe_resets_recovery_and_defers_retest(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        pool._balanced_group = "good"
        metric = pool._metric("cheap")
        metric["recovery_streak"] = 1

        with patch("retry_proxy.key_pool.time.time", return_value=100):
            pool.mark_cooldown(pool.entries[0], 30, status=503)

        self.assertEqual(metric["recovery_streak"], 0)
        self.assertEqual(metric["next_probe_at"], 400)
        self.assertEqual(metric["probe_reserved_until"], 0)

    def test_request_views_isolate_ttft_by_endpoint_and_model(self):
        pool = KeyPool(["one", "two"])
        pool.strategy = "ttft"
        chat = pool.for_request("model-a", "v1/chat/completions", "chat")
        responses = pool.for_request("model-a", "v1/responses", "responses")

        chat.record_ttft(chat.entries[0], 1.0)

        self.assertEqual(chat._metric("one")["samples"], 1)
        self.assertEqual(responses._metrics, {})

    def test_scheduler_status_reports_workload_and_recovery_state(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("cheap", "cheap", sort="0.02", group_id="cheap", group_name="Cheap"),
            KeyEntry("good", "good", sort="0.05", group_id="good", group_name="Good"),
        ]
        pool.finalize_entries()
        pool.strategy = "balanced"
        view = pool.for_request("model-a", "v1/chat/completions", "chat")
        cheap = view.pick()
        view.record_ttft(cheap, 6.0)
        view.record_ttft(cheap, 6.0)

        status = pool.scheduler_status()

        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["endpoint_family"], "chat")
        self.assertEqual(status[0]["model"], "model-a")
        self.assertEqual(status[0]["current_group_name"], "Good")
        self.assertEqual(status[0]["cheaper_groups"][0]["group_name"], "Cheap")

    def test_group_members_share_ttft_samples(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("one", "one", group_id="group"),
            KeyEntry("two", "two", group_id="group"),
        ]
        pool.finalize_entries()

        pool.record_ttft(pool.entries[0], 2.5)

        self.assertEqual([entry.ttft_ewma for entry in pool.entries], [2.5, 2.5])
        self.assertEqual([entry.ttft_samples for entry in pool.entries], [1, 1])

    def test_live_replacement_preserves_runtime_health_for_unchanged_keys(self):
        previous = KeyPool([("same", "old"), ("removed", "removed")])
        previous.entries[0].cooldown_until = 500
        previous.entries[0].total_fail = 4
        previous.entries[0].consecutive_failures = 2
        previous.entries[0].last_failure_status = 429
        previous._current = previous.entries[0]
        previous._sticky_until = 300
        pools = {"https://upstream.test": previous}

        replacement = KeyPool([("same", "new"), ("added", "added")])
        replace_key_pool("https://upstream.test/", replacement, pools)

        current = pools["https://upstream.test"]
        self.assertEqual(current.entries[0].label, "new")
        self.assertEqual(current.entries[0].cooldown_until, 500)
        self.assertEqual(current.entries[0].total_fail, 4)
        self.assertEqual(current.entries[0].consecutive_failures, 2)
        self.assertEqual(current.entries[0].last_failure_status, 429)
        self.assertIs(current._current, current.entries[0])
        self.assertEqual(current._sticky_until, 300)

    def test_inflight_failure_after_replacement_updates_the_live_pool(self):
        previous = KeyPool([("same", "old")])
        request_view = previous.for_request("model", "responses")
        inflight_entry = request_view.pick()
        pools = {"https://upstream.test": previous}

        replace_key_pool(
            "https://upstream.test", KeyPool([("same", "new")]), pools,
        )
        request_view.mark_cooldown(
            inflight_entry, 60, failure_kind="rate_limit", status=429,
        )

        live_entry = pools["https://upstream.test"].entries[0]
        self.assertIs(live_entry, inflight_entry)
        self.assertEqual(live_entry.label, "new")
        self.assertEqual(live_entry.last_failure_status, 429)
        self.assertGreater(live_entry.cooldown_until, 0)

    def test_sort_orders_entries_numerically_and_formats_log_id(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("high", "high", sort="0.2"),
            KeyEntry("equal-first", "equal-first", sort="0.03"),
            KeyEntry("low", "low", sort="0.02"),
            KeyEntry("equal-second", "equal-second", sort="0.03"),
            KeyEntry("legacy", "legacy"),
        ]

        pool.finalize_entries()

        self.assertEqual([entry.key_id for entry in pool.entries], [
            "low|0.02", "equal-first|0.03", "equal-second|0.03", "high|0.2", "legacy",
        ])
        self.assertEqual([entry.legacy_key_id for entry in pool.entries], [
            "low", "equal-first", "equal-second", "high", "legacy",
        ])

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
                self.assertEqual(pool._sticky_until, 280)
                pool.mark_success(pool.entries[0])
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
        self.assertIsNot(image_by_model, image_by_path)
        self.assertIsNot(normal, image_by_model)

        image_by_model._current = image_by_model.entries[1]
        image_by_model._sticky_until = 999
        self.assertIsNone(normal._current)
        self.assertIsNone(image_by_path._current)

    def test_specific_pool_never_falls_back_to_default_entries(self):
        pool = KeyPool([])
        normal = KeyEntry("normal", "normal")
        image = KeyEntry("image", "image", models=("gpt-image-*",))
        pool.entries = [normal, image]
        image.cooldown_until = 999
        scoped = pool.for_request("gpt-image-1", "responses")
        with patch("retry_proxy.key_pool.time.time", return_value=100):
            self.assertEqual(scoped.pick().key_id, "image")

    def test_capabilities_filter_before_manual_rules(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("openai", "openai", routing_capabilities={
                "platform": "openai", "endpoint_families": ["chat"],
            }),
            KeyEntry("anthropic", "anthropic", models=("gpt-*",),
                     routing_capabilities={
                         "platform": "anthropic", "endpoint_families": ["messages"],
                     }),
        ]

        selected = pool.for_request("gpt-4o", "v1/chat/completions", "chat")

        self.assertEqual([entry.key_id for entry in selected.entries], ["openai"])

    def test_capability_pool_does_not_fall_back_when_no_route_matches(self):
        pool = KeyPool([])
        pool.entries = [KeyEntry("anthropic", "anthropic", routing_capabilities={
            "platform": "anthropic", "endpoint_families": ["messages"],
        })]

        self.assertIsNone(pool.for_request("gpt-4o", "v1/responses", "responses"))

    def test_model_scope_and_patterns_are_hard_constraints(self):
        pool = KeyPool([])
        pool.entries = [KeyEntry("gemini", "gemini", routing_capabilities={
            "platform": "gemini", "endpoint_families": ["gemini"],
            "model_patterns": ["gemini-2.*"], "model_scopes": ["gemini_text"],
        })]

        self.assertIsNotNone(pool.for_request(
            "gemini-2.5-pro", "v1beta/models/gemini-2.5-pro:generateContent",
            "gemini", "gemini_text",
        ))
        self.assertIsNone(pool.for_request(
            "gemini-1.5-pro", "v1beta/models/gemini-1.5-pro:generateContent",
            "gemini", "gemini_text",
        ))

    def test_known_empty_model_list_rejects_modeled_requests(self):
        pool = KeyPool([])
        pool.entries = [KeyEntry("free", "free", routing_capabilities={
            "platform": "openai", "endpoint_families": ["chat"],
            "model_patterns": [], "model_list_known": True,
        })]

        self.assertIsNone(pool.for_request(
            "gpt-5.4", "v1/chat/completions", "chat",
        ))
        self.assertIsNotNone(pool.for_request(
            "", "v1/chat/completions", "chat",
        ))

    def test_group_model_lists_keep_paid_fallback(self):
        pool = KeyPool([])
        free = KeyEntry("free", "free", sort="0", routing_capabilities={
            "platform": "openai", "endpoint_families": ["chat"],
            "model_patterns": ["gpt-5.4-mini"], "model_list_known": True,
        })
        paid = KeyEntry("paid", "paid", sort="1", routing_capabilities={
            "platform": "openai", "endpoint_families": ["chat"],
            "model_patterns": ["gpt-5.4-mini", "gpt-5.4"], "model_list_known": True,
        })
        pool.entries = [free, paid]

        mini = pool.for_request("gpt-5.4-mini", "v1/chat/completions", "chat")
        full = pool.for_request("gpt-5.4", "v1/chat/completions", "chat")

        self.assertEqual([entry.key_id for entry in mini.entries], ["free|0", "paid|1"])
        self.assertEqual([entry.key_id for entry in full.entries], ["paid|1"])
        free.cooldown_until = 999
        with patch("retry_proxy.key_pool.time.time", return_value=100):
            self.assertEqual(mini.pick().key_id, "paid|1")

    def test_rejected_model_skips_only_the_rejected_group(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("free", "free", sort="0", routing_capabilities={
                "platform": "openai", "endpoint_families": ["chat"],
                "model_patterns": ["gpt-5.4"], "model_list_known": True,
                "rejected_models": ["gpt-5.4"],
            }),
            KeyEntry("paid", "paid", sort="1", routing_capabilities={
                "platform": "openai", "endpoint_families": ["chat"],
                "model_patterns": ["gpt-5.4"], "model_list_known": True,
            }),
        ]
        pool.finalize_entries()

        selected = pool.for_request("GPT-5.4", "v1/chat/completions", "chat")

        self.assertEqual([entry.key for entry in selected.entries], ["paid"])

    def test_image_models_require_image_generation_permission(self):
        pool = KeyPool([])
        pool.entries = [
            KeyEntry("text-only", "text-only", routing_capabilities={
                "platform": "openai", "endpoint_families": ["responses"],
                "model_patterns": ["gpt-image-1"], "model_list_known": True,
                "image_generation": False,
            }),
            KeyEntry("image-enabled", "image-enabled", routing_capabilities={
                "platform": "openai", "endpoint_families": ["responses"],
                "model_patterns": ["gpt-image-1"], "model_list_known": True,
                "image_generation": True,
            }),
        ]
        pool.finalize_entries()

        selected = pool.for_request("gpt-image-1", "v1/responses", "responses")

        self.assertEqual([entry.key for entry in selected.entries], ["image-enabled"])

    def test_pool_without_capabilities_preserves_legacy_default(self):
        pool = KeyPool([("legacy", "legacy")])

        selected = pool.for_request("claude-3", "v1/messages", "messages", "claude")

        self.assertEqual([entry.key_id for entry in selected.entries], ["legacy"])


class RequestClassificationTests(unittest.TestCase):
    def test_explicit_model_rejection_errors_are_recognized(self):
        self.assertTrue(is_model_rejection_response(404, json.dumps({
            "error": {"type": "invalid_request_error", "code": "model_not_found",
                      "message": "The model does not exist"},
        }).encode()))
        self.assertTrue(is_model_rejection_response(400, json.dumps({
            "error": {"message": "Unsupported model: gpt-example"},
        }).encode()))

    def test_generic_request_errors_are_not_model_rejections(self):
        cases = [
            (404, {"error": {"type": "not_found_error", "message": "Route not found"}}),
            (400, {"error": {"type": "invalid_request_error",
                              "message": "max_tokens must be positive"}}),
            (403, {"error": {"code": "model_not_found",
                              "message": "The model does not exist"}}),
        ]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                self.assertFalse(is_model_rejection_response(
                    status, json.dumps(payload).encode(),
                ))

    def test_endpoint_families(self):
        cases = {
            "v1/chat/completions": "chat",
            "responses": "responses",
            "v1/messages": "messages",
            "v1/images/generations": "images",
            "v1/embeddings": "embeddings",
            "v1/audio/transcriptions": "audio",
            "v1beta/models/gemini-2.5-pro:generateContent": "gemini",
            "v1/models": "",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_endpoint(path), expected)

    def test_model_comes_from_body_or_gemini_path(self):
        self.assertEqual(parse_request_model(b'{"model":"gpt-4o"}', "responses"), "gpt-4o")
        self.assertEqual(parse_request_model(
            b"", "v1beta/models/gemini-2.5-pro%2Btest:streamGenerateContent",
        ), "gemini-2.5-pro+test")

    def test_model_scope(self):
        self.assertEqual(classify_model_scope("claude-3-7-sonnet"), "claude")
        self.assertEqual(classify_model_scope("gemini-2.5-pro"), "gemini_text")
        self.assertEqual(classify_model_scope("gemini-2.5-image-preview"), "gemini_image")

    def test_per_entry_authentication_can_use_vendor_header(self):
        entry = KeyEntry("secret", auth={"header": "x-api-key", "scheme": ""})
        headers = headers_with_key(
            {"authorization": "Bearer client", "x-api-key": "old"},
            entry.key, entry.auth_header, entry.auth_scheme,
        )

        self.assertNotIn("authorization", {key.lower() for key in headers})
        self.assertEqual(headers["x-api-key"], "secret")


class KeyPoolCooldownWaitTests(unittest.IsolatedAsyncioTestCase):
    def test_pool_requests_always_use_serial_hedge_mode(self):
        config = SimpleNamespace(hedge_mode="stagger")
        proxy = RetryProxy(config=config, client=object())

        self.assertEqual(proxy.hedge_mode_for(None), "stagger")
        self.assertEqual(proxy.hedge_mode_for(KeyPool([("key", "key")])), "off")

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

    async def test_pick_timeout_does_not_wait_for_a_long_cooldown(self):
        pool = KeyPool([("key", "key")])
        pool.entries[0].cooldown_until = 130

        with patch("retry_proxy.key_pool.time.time", return_value=100), \
                patch("retry_proxy.retry.time.time", return_value=100), \
                patch("retry_proxy.retry.time.monotonic", side_effect=[100, 100]), \
                patch("retry_proxy.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(KeyPoolWaitTimeout):
                await _pick_key(pool, wait_timeout=0.5)

        sleep.assert_awaited_once_with(0.5)

    async def test_proxy_turns_key_wait_timeout_into_a_failed_result(self):
        config = SimpleNamespace(hedge_mode="off", max_retries=2, key_pool_wait_timeout=0.5)
        proxy = RetryProxy(config=config, client=object())

        with patch("retry_proxy.retry._pick_key", new_callable=AsyncMock,
                   side_effect=KeyPoolWaitTimeout(30, 0.5)):
            result = await proxy.request(
                "POST", "https://upstream.test/responses", {}, b'{}',
                "responses", "test", "model", KeyPool([("key", "key")]),
            )

        self.assertIsNone(result.response)
        self.assertEqual(result.total_sent, 0)
        self.assertIn("wait limit 0.5s", result.failure_reason)

    async def test_retry_cooldown_wait_is_also_bounded(self):
        pool = KeyPool([("key", "key")])
        config = SimpleNamespace(
            hedge_mode="off", max_retries=60, retry_interval=1,
            retry_interval_429=5, retry_backoff=False, retry_backoff_max=60,
            retry_backoff_429=True, retry_backoff_max_429=60,
            key_cooldown=30, key_cooldown_5xx=30, key_cooldown_429=60,
            key_cooldown_auth=1800, key_cooldown_max=3600,
            key_cooldown_backoff=True, key_pool_wait_timeout=0.5,
        )
        response = httpx.Response(
            503, json={"error": {"message": "temporarily unavailable"}},
            request=httpx.Request("POST", "https://upstream.test/responses"),
        )
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(return_value=response)

        with patch("retry_proxy.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await proxy.request(
                "POST", "https://upstream.test/responses", {}, b'{}',
                "responses", "test", "model", pool,
            )

        self.assertIsNone(result.response)
        self.assertEqual(result.total_sent, 1)
        self.assertIn("wait limit 0.5s", result.failure_reason)
        sleep.assert_awaited_once_with(0.5)

    async def test_global_race_mode_still_uses_serial_pool_circuit(self):
        pool = KeyPool([("key", "key"), ("backup", "backup")])
        config = SimpleNamespace(
            hedge_mode="race", max_concurrent=1, max_retries=1,
            retry_interval=1, retry_interval_429=5,
            retry_backoff=False, retry_backoff_max=60,
            retry_backoff_429=True, retry_backoff_max_429=60,
            key_cooldown=30, key_cooldown_5xx=30, key_cooldown_429=60,
            key_cooldown_auth=1800, key_cooldown_max=3600, key_cooldown_backoff=True,
        )
        response = httpx.Response(
            503, json={"error": {"message": "temporarily unavailable"}},
            request=httpx.Request("POST", "https://upstream.test"),
        )
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(return_value=response)

        with patch("retry_proxy.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await proxy.request("POST", "https://upstream.test", {}, b"{}",
                                         "v1/chat", "test", "model", pool)

        self.assertIsNone(result.response)
        self.assertEqual(proxy._send.await_count, 1)
        self.assertTrue(pool.entries[0].cooldown_until > 0)
        self.assertEqual(pool.entries[0].last_failure_status, 503)

    async def test_auth_failure_returns_immediately_when_scoped_pool_is_exhausted(self):
        pool = KeyPool([("key", "image")])
        config = SimpleNamespace(
            hedge_mode="off", max_retries=60, retry_interval=1,
            retry_interval_429=5, retry_backoff=False, retry_backoff_max=60,
            retry_backoff_429=True, retry_backoff_max_429=60,
            key_cooldown=30, key_cooldown_5xx=30, key_cooldown_429=60,
            key_cooldown_auth=1800, key_cooldown_max=3600, key_cooldown_backoff=True,
        )
        response = httpx.Response(
            403, json={"error": {"message": "image option is not allowed"}},
            request=httpx.Request("POST", "https://upstream.test/images/generations"),
        )
        proxy = RetryProxy(config=config, client=object())
        proxy._send = AsyncMock(return_value=response)

        with patch("retry_proxy.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await proxy.request(
                "POST", "https://upstream.test/images/generations", {},
                b'{"model":"gpt-image-2"}', "images/generations", "test", "gpt-image-2", pool,
            )

        self.assertIs(result.response, response)
        self.assertEqual(result.last_status, 403)
        self.assertEqual(result.total_sent, 1)
        self.assertEqual(pool.entries[0].last_failure_status, 403)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
