import unittest

from retry_proxy.stats import _agg_by_key, compute_key_pool_stats


class KeyAvailabilityStatsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
