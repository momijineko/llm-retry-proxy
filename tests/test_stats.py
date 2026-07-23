import unittest

from retry_proxy.stats import _agg_by, _agg_by_key, compute_key_pool_stats


class KeyAvailabilityStatsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
