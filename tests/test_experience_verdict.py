import unittest

from retry_proxy.experience_data import _parse_experience_payload
from retry_proxy.pool_sync import PoolSyncManager
from retry_proxy.sync_adapters import PoolSyncError


class ExperienceVerdictTests(unittest.TestCase):
    def config(self, mode="pass_ratio"):
        return PoolSyncManager._normalize_experience_source(
            "https://upstream.test/overview", query_params={"hours": 24},
            auth_mode="source_session", transform={
                "items_path": "data.targets", "id_path": "group",
                "ttft_path": "math.latest.first_token_ms",
                "detection_path": "math.latest.status",
                "detection_map": {"pass": "智力正常", "degraded": "偶发答错",
                                  "critical": "疑似降智"},
                "pass_path": "math.pass", "fail_path": "math.fail",
                "detection_mode": mode,
            })

    def parse(self, raw, config=None):
        return _parse_experience_payload(
            {"data": {"targets": [{"group": "plus", **raw}]}},
            (config or self.config())["transform"],
        )[0]

    def test_boundaries_and_routing_ignore_latest_and_error_counts(self):
        for passed, failed, status, label in (
                (90, 10, "pass", "智力正常"),
                (89, 11, "degraded", "偶发答错"),
                (70, 30, "degraded", "偶发答错"),
                (69, 31, "critical", "疑似降智"),
                (31, 17, "critical", "疑似降智"),
                (0, 0, "unknown", "暂无数据")):
            with self.subTest(passed=passed, failed=failed):
                item = self.parse({"math": {
                    "pass": passed, "fail": failed, "error": 999,
                    "running": 100, "pass_rate": 100,
                    "latest": {"status": "pass", "first_token_ms": 2000},
                }})
                self.assertEqual(item["detection_status"], status)
                self.assertEqual(item["detection_label"], label)
                self.assertEqual(item["ttft"], 2)
                self.assertEqual(item["samples"], 1)
                source = {"experience_source": self.config(),
                          "experience_items": [item],
                          "experience_mappings": {"local": "plus"}}
                self.assertEqual(
                    PoolSyncManager._detection_disabled_group_ids(source),
                    set() if status in ("pass", "unknown") else {"local"},
                )

    def test_arbitrary_paths_and_custom_thresholds(self):
        config = self.config()
        config["transform"].update(
            pass_path="checks.good", fail_path="checks.bad",
            healthy_threshold=80, warning_threshold=50, detection_map={})
        for good, bad, expected in ((8, 2, "pass"), (5, 5, "degraded"),
                                    (4, 6, "critical")):
            item = self.parse({"checks": {"good": good, "bad": bad}}, config)
            self.assertEqual(item["detection_status"], expected)

    def test_configuration_validation(self):
        for overrides in ({"pass_path": ""}, {"fail_path": "a[0]"},
                          {"healthy_threshold": 101},
                          {"warning_threshold": -1},
                          {"warning_threshold": 95},
                          {"healthy_threshold": True},
                          {"healthy_threshold": float("nan")}):
            with self.subTest(overrides=overrides), self.assertRaises(PoolSyncError):
                PoolSyncManager._normalize_experience_source(
                    "https://upstream.test/overview", query_params={},
                    transform={**self.config()["transform"], **overrides})

    def test_invalid_statistics_fail_refresh_instead_of_becoming_unknown(self):
        for raw in ({}, {"math": {}},
                    {"math": {"pass": -1, "fail": 0}},
                    {"math": {"pass": True, "fail": 0}},
                    {"math": {"pass": "90", "fail": 10}},
                    {"math": {"pass": float("nan"), "fail": 0}}):
            with self.subTest(raw=raw), self.assertRaises(PoolSyncError):
                self.parse(raw)

    def test_legacy_field_mode_and_invalid_mode(self):
        config = self.config("field")
        del config["transform"]["detection_mode"]
        item = self.parse({"math": {"latest": {"status": "pass"}}}, config)
        self.assertEqual(item["detection_label"], "智力正常")
        with self.assertRaises(PoolSyncError):
            self.config("arbitrary_expression")
