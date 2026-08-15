import unittest
from types import SimpleNamespace


from retry_proxy.application import app
from retry_proxy.routes import (RouteRegistry, build_proxy_url,
                                normalize_route_prefix)


class ProxyUrlVersionCollapseTests(unittest.TestCase):
    """下游版本优先：上游基地址与下游路径都带 /vN 时消除 /v1/v1 叠加"""

    def test_downstream_version_wins_over_trailing_base_version(self):
        self.assertEqual(
            build_proxy_url("https://opencode.ai/zen/go/v1", "v1/chat/completions"),
            "https://opencode.ai/zen/go/v1/chat/completions",
        )

    def test_different_downstream_version_replaces_base_version(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test/v1", "v2/chat/completions"),
            "https://upstream.test/v2/chat/completions",
        )

    def test_base_without_version_keeps_downstream_version(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test", "v1/chat/completions"),
            "https://upstream.test/v1/chat/completions",
        )

    def test_path_without_version_keeps_base_version(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test/v1", "chat/completions"),
            "https://upstream.test/v1/chat/completions",
        )

    def test_non_version_words_are_not_collapsed(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test/v1", "v1beta/models"),
            "https://upstream.test/v1/v1beta/models",
        )

    def test_empty_remaining_returns_base(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test/v1", ""),
            "https://upstream.test/v1",
        )

    def test_version_only_path_collapses_to_single_version(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test/v1", "v1"),
            "https://upstream.test/v1",
        )

    def test_case_insensitive_version_segment(self):
        self.assertEqual(
            build_proxy_url("https://upstream.test/v1", "V1/chat"),
            "https://upstream.test/V1/chat",
        )


class RouteRegistryTests(unittest.TestCase):
    def config(self, extras=""):
        return SimpleNamespace(
            extra_upstreams=extras,
            upstream_url="https://default.test/v1",
            provider="default-provider",
        )

    def test_managed_route_is_matched_and_strips_prefix(self):
        registry = RouteRegistry(self.config())

        registry.register("source-1", "/managed", "https://managed.test", "managed-provider")

        self.assertEqual(
            registry.match("managed/v1/chat/completions"),
            ("https://managed.test", "managed-provider", "v1/chat/completions"),
        )
        self.assertEqual(
            registry.match("v1/chat/completions"),
            ("https://default.test/v1", "default-provider", "v1/chat/completions"),
        )

    def test_longest_prefix_wins_across_environment_and_managed_routes(self):
        registry = RouteRegistry(self.config("/api|https://env.test|env"))

        registry.register("source-1", "/api/special", "https://special.test", "special")

        self.assertEqual(registry.match("api/special/models")[0], "https://special.test")
        self.assertEqual(registry.match("api/models")[0], "https://env.test")

    def test_environment_route_cannot_be_overridden(self):
        registry = RouteRegistry(self.config("/fixed|https://env.test|env"))

        with self.assertRaisesRegex(ValueError, "EXTRA_UPSTREAMS"):
            registry.register("source-1", "/fixed", "https://other.test", "other")

        registry.register("source-1", "/fixed", "https://env.test", "managed")
        self.assertEqual(registry.match("fixed/models")[:2], ("https://env.test", "env"))

    def test_same_provider_managed_route_overrides_environment_target(self):
        registry = RouteRegistry(self.config("/aihub|http://57.131.13.16:8080|aihub"))

        registry.register("source-1", "/aihub", "https://account.aihub.test", "aihub")

        self.assertEqual(
            registry.environment_upstream(
                "/aihub", "https://account.aihub.test", "aihub",
            ),
            "https://account.aihub.test",
        )
        self.assertEqual(
            registry.match("aihub/v1/models")[:2],
            ("https://account.aihub.test", "aihub"),
        )

        registry.unregister("source-1")

        self.assertEqual(
            registry.match("aihub/v1/models")[:2],
            ("http://57.131.13.16:8080", "aihub"),
        )

    def test_prefix_normalization_and_validation(self):
        self.assertEqual(normalize_route_prefix("example/"), "/example")
        self.assertEqual(normalize_route_prefix("/"), "")
        with self.assertRaises(ValueError):
            normalize_route_prefix("https://example.test")


class ApplicationTransportRouteTests(unittest.TestCase):
    def test_proxy_registers_http_catchall(self):
        self.assertTrue(any(
            getattr(route, "path", "") == "/{path:path}"
            and "POST" in (getattr(route, "methods", None) or set())
            for route in app.routes
        ))


if __name__ == "__main__":
    unittest.main()
