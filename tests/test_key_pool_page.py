import unittest
from pathlib import Path


class KeyPoolPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "key_pool.html"
        ).read_text(encoding="utf-8")

    def test_multiple_sources_render_as_accessible_accordion(self):
        self.assertIn('data-toggle-source="${esc(s.id)}"', self.html)
        self.assertIn('aria-expanded="${expanded?\'true\':\'false\'}"', self.html)
        self.assertIn('aria-controls="${bodyId}"', self.html)
        self.assertIn('class="source-body"', self.html)

    def test_accordion_keeps_one_expanded_source_across_renders(self):
        self.assertIn("expandedSourceId=null", self.html)
        self.assertIn("function normalizeExpandedSource(sources)", self.html)
        self.assertIn("expandedSourceId=opening?String(sourceId):''", self.html)
        self.assertIn("item.classList.toggle('collapsed',!expanded)", self.html)


if __name__ == "__main__":
    unittest.main()
