import unittest
from pathlib import Path


class StatsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "stats.html").read_text(
            encoding="utf-8"
        )

    def test_key_availability_table_shows_cache_hit_rate(self):
        self.assertIn('<th class="num">缓存命中率</th>', self.html)
        self.assertIn("keyCacheRateCell(d.cached_tokens,d.prompt_tokens)", self.html)
        self.assertIn("function keyCacheRateCell(cached,prompt)", self.html)


if __name__ == "__main__":
    unittest.main()
