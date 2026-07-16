import unittest
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy.key_pool import KeyPool


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


if __name__ == "__main__":
    unittest.main()
