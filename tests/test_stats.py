import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy.api import _cumulative
from retry_proxy.log_store import RetryLogStore
from retry_proxy.pool_sync import _mask_key
from retry_proxy.stats import (_normalize_provider, _agg_by, _agg_by_key,
                               compute_key_pool_stats, compute_stats)


class KeyAvailabilityStatsTests(unittest.TestCase):
    def test_short_key_mask_does_not_leak_full_key(self):
        # Regression: masking short keys with [:7]+"..."+[-4:] overlapped and
        # revealed the whole key (e.g. "abc" -> "abc...abc").
        self.assertEqual(_mask_key("abc"), "ab***")
        self.assertEqual(_mask_key(""), "***")
        self.assertEqual(_mask_key("sk-short"), "sk***")
        # Normal-length keys keep the head/tail mask
        self.assertEqual(_mask_key("sk-secret-one"), "sk-secr...-one")
        self.assertNotIn("secret", _mask_key("sk-secret-one"))

    def test_anthropic_provider_is_no_longer_relabelled(self):
        # Regression: "anthropic" used to be hardcoded to "xfyun", silently
        # relabelling Anthropic traffic. Now only the Chinese display-name
        # alias remains by default.
        self.assertEqual(_normalize_provider("anthropic"), "anthropic")
        self.assertEqual(_normalize_provider("讯飞星辰 Coding Plan"), "xfyun")
        self.assertEqual(_normalize_provider("custom"), "custom")

    def test_client_cancellation_is_excluded_from_failure_and_availability(self):
        records = [
            {"provider": "test", "model": "model", "final_status": 200,
             "upstream_status": 200, "succeeded": True, "first_ok": True, "retries": 0},
            {"provider": "test", "model": "model", "final_status": 503,
             "upstream_status": 503, "succeeded": False, "first_ok": False, "retries": 0},
            {"provider": "test", "model": "model", "final_status": 200,
             "upstream_status": 200, "succeeded": False, "first_ok": True, "retries": 0,
             "stream_status": "cancelled"},
        ]

        aggregate = _agg_by(records, "model", "model")[0]
        stats = compute_stats(records, "today", {})

        self.assertEqual(aggregate["requests"], 3)
        self.assertEqual(aggregate["cancelled"], 1)
        self.assertEqual(aggregate["failed"], 1)
        self.assertEqual(aggregate["availability_pct"], 50)
        self.assertEqual(stats["summary"]["total_requests"], 3)
        self.assertEqual(stats["summary"]["cancelled_requests"], 1)
        self.assertEqual(stats["summary"]["failed_requests"], 1)
        self.assertEqual(stats["summary"]["success_rate"], 0.5)
        self.assertEqual(stats["summary"]["availability_pct"], 50)

    def test_failure_streak_uses_chronological_order(self):
        # Records arrive in non-chronological file order; streaks must be
        # evaluated by timestamp. Here the later (by ts) record succeeds, so the
        # true worst failure streak is 1, not 2.
        records = [
            {"ts": "2026-07-27T10:00:00", "provider": "test", "model": "m",
             "final_status": 503, "upstream_status": 503, "succeeded": False,
             "first_ok": False, "retries": 0},
            {"ts": "2026-07-27T09:00:00", "provider": "test", "model": "m",
             "final_status": 200, "upstream_status": 200, "succeeded": True,
             "first_ok": True, "retries": 0},
            {"ts": "2026-07-27T11:00:00", "provider": "test", "model": "m",
             "final_status": 503, "upstream_status": 503, "succeeded": False,
             "first_ok": False, "retries": 0},
        ]
        stats = compute_stats(records, "today", {})
        # Chronological: 09:00 ok, 10:00 fail, 11:00 fail -> worst streak 2
        self.assertEqual(stats["availability"]["worst_failure_streak"], 2)

    def test_failure_streak_unsorted_records_stay_correct(self):
        # Same records shuffled so that two failures are non-adjacent in file
        # order but adjacent chronologically.
        records = [
            {"ts": "2026-07-27T10:00:00", "provider": "test", "model": "m",
             "final_status": 503, "upstream_status": 503, "succeeded": False,
             "first_ok": False, "retries": 0},
            {"ts": "2026-07-27T09:00:00", "provider": "test", "model": "m",
             "final_status": 200, "upstream_status": 200, "succeeded": True,
             "first_ok": True, "retries": 0},
            {"ts": "2026-07-27T11:00:00", "provider": "test", "model": "m",
             "final_status": 503, "upstream_status": 503, "succeeded": False,
             "first_ok": False, "retries": 0},
        ]
        stats = compute_stats(records, "today", {})
        self.assertEqual(stats["availability"]["worst_failure_streak"], 2)

    def test_cumulative_summary_tracks_cancellation_as_neutral(self):
        store = RetryLogStore()
        summary = store._new_summary()
        store._update(summary, {
            "provider": "test", "model": "model", "key_id": "key",
            "upstream_status": 200, "final_status": 200, "retries": 0,
            "succeeded": False, "stream_status": "cancelled",
        })

        cumulative = _cumulative(summary)

        self.assertEqual(cumulative["total_requests"], 1)
        self.assertEqual(cumulative["cancelled"], 1)
        self.assertEqual(cumulative["failed"], 0)
        self.assertEqual(cumulative["availability_pct"], 0)

    def test_stream_failure_overrides_successful_http_status(self):
        records = [{
            "provider": "test",
            "model": "model",
            "final_status": 200,
            "upstream_status": 200,
            "stream_error_status": 502,
            "succeeded": False,
            "retries": 0,
        }]

        result = _agg_by(records, "model", "model")[0]

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["availability_pct"], 0)
        self.assertEqual(result["dominant_fail_status"], 502)

    def test_attempt_trace_attributes_failures_to_the_key_that_was_used(self):
        records = [{
            "key_id": "premium",
            "final_status": 200,
            "retries": 1,
            "key_attempts": [
                {"key_id": "cheap", "available": False},
                {"key_id": "premium", "available": True},
            ],
        }]

        result = {item["key_id"]: item for item in _agg_by_key(records)}

        self.assertEqual(result["cheap"]["availability_pct"], 0)
        self.assertEqual(result["cheap"]["failed_attempts"], 1)
        self.assertEqual(result["premium"]["availability_pct"], 100)
        self.assertEqual(result["premium"]["requests"], 1)

    def test_key_stats_sum_cache_tokens_for_the_final_key(self):
        records = [
            {
                "key_id": "warm",
                "final_status": 200,
                "prompt_tokens": 1000,
                "cached_tokens": 250,
                "total_tokens": 1100,
                "key_attempts": [
                    {"key_id": "cold", "available": False},
                    {"key_id": "warm", "available": True},
                ],
            },
            {
                "key_id": "warm",
                "final_status": 200,
                "prompt_tokens": 3000,
                "cached_tokens": 1750,
                "total_tokens": 3200,
                "key_attempts": [{"key_id": "warm", "available": True}],
            },
        ]

        result = {item["key_id"]: item for item in _agg_by_key(records)}

        self.assertEqual(result["warm"]["prompt_tokens"], 4000)
        self.assertEqual(result["warm"]["cached_tokens"], 2000)
        self.assertEqual(result["warm"]["total_tokens"], 4300)
        self.assertEqual(result["cold"]["prompt_tokens"], 0)

    def test_host_errors_are_excluded_from_availability(self):
        records = [{
            "key_id": "primary",
            "final_status": 200,
            "retries": 1,
            "key_attempts": [
                {"key_id": "primary", "available": None},
                {"key_id": "primary", "available": True},
            ],
        }]

        result = _agg_by_key(records)[0]

        self.assertEqual(result["availability_pct"], 100)
        self.assertEqual(result["ignored_attempts"], 1)

        only_host_error = _agg_by_key([{
            "key_id": "primary",
            "final_status": 503,
            "retries": 0,
            "key_attempts": [{"key_id": "primary", "available": None}],
        }])[0]
        self.assertIsNone(only_host_error["availability_pct"])

    def test_legacy_records_fall_back_to_final_request_result(self):
        records = [
            {"key_id": "legacy", "final_status": 200, "retries": 0},
            {"key_id": "legacy", "final_status": 503, "retries": 2},
        ]

        result = _agg_by_key(records)[0]

        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["legacy_attempts"], 2)
        self.assertEqual(result["availability_pct"], 50)
        self.assertEqual(result["request_availability_pct"], 50)

    def test_pools_are_separate_and_only_configured_keys_are_returned(self):
        configs = [
            {"id": "https://a.test", "upstream": "https://a.test", "provider": "a", "keys": [
                {"key_id": "shared", "cooled": True, "cooldown_remaining": 12.5},
                {"key_id": "a-only", "cooled": False, "cooldown_remaining": 0},
            ]},
            {"id": "https://b.test", "upstream": "https://b.test", "provider": "b", "keys": ["shared", "b-only"]},
        ]
        records = [
            {"provider": "a", "key_pool": "https://a.test", "key_id": "shared", "final_status": 503,
             "retries": 0, "key_attempts": [{"key_id": "shared", "available": False}]},
            {"provider": "b", "key_pool": "https://b.test", "key_id": "shared", "final_status": 200,
             "retries": 0, "key_attempts": [{"key_id": "shared", "available": True}]},
            {"provider": "a", "key_id": "a-only", "final_status": 200, "retries": 0},
            {"provider": "other", "key_id": "ghost", "final_status": 200, "retries": 0},
        ]

        pools = {pool["id"]: pool for pool in compute_key_pool_stats(records, configs)}
        a_keys = {item["key_id"]: item for item in pools["https://a.test"]["keys"]}
        b_keys = {item["key_id"]: item for item in pools["https://b.test"]["keys"]}

        self.assertEqual(set(a_keys), {"shared", "a-only"})
        self.assertEqual(set(b_keys), {"shared", "b-only"})
        self.assertEqual(a_keys["shared"]["availability_pct"], 0)
        self.assertTrue(a_keys["shared"]["cooled"])
        self.assertEqual(b_keys["shared"]["availability_pct"], 100)
        self.assertEqual(a_keys["a-only"]["availability_pct"], 100)
        self.assertIsNone(b_keys["b-only"]["availability_pct"])

    def test_ambiguous_legacy_key_is_not_assigned_to_multiple_pools(self):
        configs = [
            {"id": "pool-1", "provider": "same", "keys": ["shared"]},
            {"id": "pool-2", "provider": "same", "keys": ["shared"]},
        ]
        records = [{"provider": "same", "key_id": "shared", "final_status": 200, "retries": 0}]

        pools = compute_key_pool_stats(records, configs)

        self.assertTrue(all(pool["keys"][0]["attempts"] == 0 for pool in pools))

    def test_latest_failure_marks_key_unavailable_despite_good_history(self):
        configs = [{"id": "pool", "provider": "p", "keys": ["key-1"]}]
        records = [
            {"ts": "2026-07-17T10:00:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 200, "key_attempts": [{"key_id": "key-1", "available": True}]},
            {"ts": "2026-07-17T10:01:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 200, "key_attempts": [{"key_id": "key-1", "available": True}]},
            {"ts": "2026-07-17T10:02:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 401, "key_attempts": [{"key_id": "key-1", "available": False}]},
        ]

        key = compute_key_pool_stats(records, configs)[0]["keys"][0]

        self.assertEqual(key["availability_pct"], 66.67)
        self.assertEqual(key["health_status"], "unavailable")
        self.assertFalse(key["latest_available"])
        self.assertEqual(key["consecutive_failures"], 1)

    def test_recent_health_is_independent_from_selected_stats_range(self):
        configs = [{"id": "pool", "provider": "p", "keys": ["key-1"]}]
        selected_records = [
            {"ts": "2026-07-01T10:00:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 200, "key_attempts": [{"key_id": "key-1", "available": True}]},
        ]
        health_records = [
            {"ts": "2026-07-17T10:00:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 503, "key_attempts": [{"key_id": "key-1", "available": False}]},
            {"ts": "2026-07-17T10:01:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 503, "key_attempts": [{"key_id": "key-1", "available": False}]},
        ]

        key = compute_key_pool_stats(selected_records, configs, health_records=health_records)[0]["keys"][0]

        self.assertEqual(key["availability_pct"], 100)
        self.assertEqual(key["health_status"], "unavailable")
        self.assertEqual(key["consecutive_failures"], 2)

    def test_neutral_latest_observation_does_not_retain_old_failure_status(self):
        configs = [{"id": "pool", "provider": "p", "keys": ["key-1"]}]
        records = [
            {"ts": "2026-07-17T10:00:00", "provider": "p", "key_pool": "pool",
             "key_id": "key-1", "final_status": 503,
             "key_attempts": [{"key_id": "key-1", "available": False}]},
            # The response headers succeeded, but the stream timed out before its
            # first event; this neutral sample is excluded from the availability
            # percentage.
            {"ts": "2026-07-17T10:01:00", "provider": "p", "key_pool": "pool",
             "key_id": "key-1", "final_status": 504, "stream_status": "first_event_timeout",
             "key_attempts": [{"key_id": "key-1", "available": None}]},
        ]

        key = compute_key_pool_stats(records, configs, health_records=records)[0]["keys"][0]

        self.assertEqual(key["availability_pct"], 0)
        self.assertEqual(key["latest_available"], False)
        self.assertEqual(key["health_status"], "available")

    def test_generic_neutral_observation_does_not_clear_old_failure_status(self):
        configs = [{"id": "pool", "provider": "p", "keys": ["key-1"]}]
        records = [
            {"ts": "2026-07-17T10:00:00", "provider": "p", "key_pool": "pool",
             "key_id": "key-1", "key_attempts": [{"key_id": "key-1", "available": False}]},
            {"ts": "2026-07-17T10:01:00", "provider": "p", "key_pool": "pool",
             "key_id": "key-1", "stream_status": "cancelled",
             "key_attempts": [{"key_id": "key-1", "available": None}]},
        ]

        key = compute_key_pool_stats(records, configs, health_records=records)[0]["keys"][0]

        self.assertEqual(key["health_status"], "unavailable")

    def test_active_cooldown_is_reported_as_open_circuit(self):
        configs = [{"id": "pool", "provider": "p", "keys": [
            {"key_id": "key-1", "cooled": True, "cooldown_remaining": 12},
        ]}]
        records = [
            {"ts": "2026-07-17T10:00:00", "provider": "p", "key_pool": "pool", "key_id": "key-1",
             "final_status": 200, "key_attempts": [{"key_id": "key-1", "available": True}]},
        ]

        key = compute_key_pool_stats(records, configs)[0]["keys"][0]

        self.assertEqual(key["health_status"], "circuit_open")

    def test_runtime_failure_state_remains_unavailable_after_cooldown(self):
        configs = [{"id": "pool", "provider": "p", "keys": [
            {"key_id": "key-1", "cooled": False, "consecutive_failures": 2,
             "last_failure_status": 401, "last_cooldown_s": 3600},
        ]}]

        key = compute_key_pool_stats([], configs, health_records=[])[0]["keys"][0]

        self.assertEqual(key["health_status"], "unavailable")
        self.assertEqual(key["consecutive_failures"], 2)

    def test_legacy_key_id_is_merged_into_sorted_key_stats(self):
        configs = [{"id": "pool", "provider": "p", "keys": [
            {"key_id": "cheap|0.02", "legacy_key_id": "cheap", "sort": "0.02", "cooled": False},
        ]}]
        records = [
            {"ts": "2026-07-17T09:00:00", "provider": "p", "key_pool": "pool", "key_id": "cheap",
             "final_status": 200, "key_attempts": [{"key_id": "cheap", "available": True}]},
            {"ts": "2026-07-17T10:00:00", "provider": "p", "key_pool": "pool", "key_id": "cheap|0.02",
             "final_status": 503, "key_attempts": [{"key_id": "cheap|0.02", "available": False}]},
        ]

        key = compute_key_pool_stats(records, configs)[0]["keys"][0]

        self.assertEqual(key["key_id"], "cheap|0.02")
        self.assertEqual(key["attempts"], 2)
        self.assertEqual(key["availability_pct"], 50)
        self.assertEqual(key["health_status"], "unavailable")


class LogStoreFlushTests(unittest.IsolatedAsyncioTestCase):
    def _record(self, succeeded=True):
        return {
            "ts": "2026-07-27T00:00:00.000",
            "provider": "test", "model": "model", "key_id": "key",
            "upstream_status": 200, "final_status": 200, "retries": 0,
            "succeeded": succeeded, "first_ok": True,
        }

    async def test_summary_is_not_persisted_on_every_write_within_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(
                log_dir=tmp, log_retention_days=30,
                legacy_log_file=os.path.join(tmp, "retry_log.jsonl"),
                summary_file=os.path.join(tmp, "_summary.json"),
            )
            store = RetryLogStore()
            with patch("retry_proxy.log_store.settings", config), \
                    patch("retry_proxy.log_store.is_excluded_path", return_value=False):
                store.initialize()
                save_count = [0]
                original_save = store._save

                def counting_save():
                    save_count[0] += 1
                    original_save()

                store._save = counting_save
                for _ in range(10):
                    await store.write(self._record())
                # Within the flush interval, _save should run at most once
                self.assertLessEqual(save_count[0], 1)
                # In-memory summary reflects all writes
                self.assertEqual(store.summary_cache["total_requests"], 10)

    async def test_flush_forces_persistence_of_pending_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(
                log_dir=tmp, log_retention_days=30,
                legacy_log_file=os.path.join(tmp, "retry_log.jsonl"),
                summary_file=os.path.join(tmp, "_summary.json"),
            )
            store = RetryLogStore()
            with patch("retry_proxy.log_store.settings", config), \
                    patch("retry_proxy.log_store.is_excluded_path", return_value=False):
                store.initialize()
                for _ in range(5):
                    await store.write(self._record())
                store.flush()
                # After flush, the persisted file should contain the full count
                with open(os.path.join(tmp, "_summary.json"), encoding="utf-8") as f:
                    persisted = json.load(f)
                self.assertEqual(persisted["total_requests"], 5)

    async def test_restart_replays_jsonl_tail_after_unflushed_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(
                log_dir=tmp, log_retention_days=30,
                legacy_log_file=os.path.join(tmp, "retry_log.jsonl"),
                summary_file=os.path.join(tmp, "_summary.json"),
            )
            with patch("retry_proxy.log_store.settings", config), \
                    patch("retry_proxy.log_store.is_excluded_path", return_value=False):
                store = RetryLogStore()
                store.initialize()
                await store.write(self._record())
                store.flush()
                await store.write({**self._record(), "ts": "2026-07-27T00:00:01.000"})

                restored = RetryLogStore()
                restored.initialize()

                self.assertEqual(len(restored.load(0)), 2)
                self.assertEqual(restored.summary["total_requests"], 2)
                with open(config.summary_file, encoding="utf-8") as f:
                    persisted = json.load(f)
                self.assertEqual(persisted["total_requests"], 2)
                self.assertEqual(persisted["version"], 7)

    async def test_version_six_summary_migration_replays_unflushed_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(
                log_dir=tmp, log_retention_days=30,
                legacy_log_file=os.path.join(tmp, "retry_log.jsonl"),
                summary_file=os.path.join(tmp, "_summary.json"),
            )
            with patch("retry_proxy.log_store.settings", config), \
                    patch("retry_proxy.log_store.is_excluded_path", return_value=False):
                store = RetryLogStore()
                store.initialize()
                await store.write(self._record())
                store.flush()
                with open(config.summary_file, encoding="utf-8") as f:
                    version_six = json.load(f)
                version_six["version"] = 6
                version_six.pop("log_offsets", None)
                with open(config.summary_file, "w", encoding="utf-8") as f:
                    json.dump(version_six, f)
                await store.write({**self._record(), "ts": "2026-07-27T00:00:01.000"})

                restored = RetryLogStore()
                restored.initialize()

                self.assertEqual(restored.summary["total_requests"], 2)


if __name__ == "__main__":
    unittest.main()
