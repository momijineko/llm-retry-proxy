import unittest
from types import SimpleNamespace

from retry_proxy.routes import RouteRegistry, normalize_route_prefix


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


if __name__ == "__main__":
    unittest.main()
