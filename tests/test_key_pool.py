import unittest
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy.key_pool import KeyEntry, KeyPool


class KeyPoolStickyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
