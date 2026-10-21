"""管理页共享样式/脚本与运行时导航裁剪的回归测试。"""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retry_proxy.api import (_admin_page_html, _with_admin_assets,
                             _with_request_diagnostics_nav,
                             _with_settings_nav, create_handlers)

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("stats.html", "logs.html", "key_pool.html", "settings.html", "requests.html")
LIGHT_PAGES = ("stats.html", "key_pool.html", "settings.html", "requests.html")


class AdminAssetWiringTests(unittest.TestCase):
    def test_each_page_references_shared_assets_once(self):
        for name in PAGES:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertEqual(text.count('<link rel="stylesheet" href="/admin/assets/admin_theme.css">'), 1, name)
            self.assertEqual(text.count('<script src="/admin/assets/admin_ui.js" defer></script>'), 1, name)
            # 共享样式必须在页面内联样式之前，页面规则才能覆盖
            self.assertLess(text.index('admin_theme.css'), text.index('<style>'), name)

    def test_light_pages_set_theme_before_first_paint(self):
        # 首屏脚本位于内联样式之前，避免深色主题闪白
        for name in LIGHT_PAGES:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('localStorage.getItem("lrp.theme")', text, name)
            self.assertLess(text.index('lrp.theme'), text.index('<style>'), name)

    def test_theme_supports_dark_and_compact_density(self):
        css = (ROOT / "admin_theme.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn('html[data-density="compact"]', css)
        self.assertIn("prefers-reduced-motion", css)
        # 深色主题必须覆盖正文色，避免浅色文字落在深色底上不可读
        dark = css.split('html[data-theme="dark"]', 1)[1]
        for token in ("--bg:", "--card:", "--text:", "--border:", "--muted:"):
            self.assertIn(token, dark)

    def test_theme_script_exposes_prefs_and_mounts_once(self):
        script = (ROOT / "admin_ui.js").read_text(encoding="utf-8")
        for token in ("AdminPrefs", "data-theme-value", "data-density-value",
                      "resolvedTheme", "prefers-color-scheme"):
            self.assertIn(token, script)
        # 切换器只能挂载一次，避免 header 重复渲染
        self.assertIn("if (!host || host.querySelector('.admin-prefs')) return;", script)


class AdminAssetRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_assets_are_served_with_expected_content_types(self):
        handlers = create_handlers(None, None)
        asset = handlers[8]
        css = await asset("admin_theme.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.media_type)
        self.assertIn(b"--primary", css.body)

        script = await asset("admin_ui.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("javascript", script.media_type)
        self.assertIn(b"AdminPrefs", script.body)

    async def test_unknown_asset_names_are_rejected(self):
        asset = create_handlers(None, None)[8]
        for name in ("../main.py", "retry_proxy/config.py", "missing.css", ""):
            with self.subTest(name=name):
                self.assertEqual((await asset(name)).status_code, 404)


class AdminNavRuntimeTests(unittest.TestCase):
    def test_settings_nav_is_stripped_only_when_disabled(self):
        html = '<nav><a href="/key-pools">号池</a><a href="/settings">配置</a></nav>'
        with patch("retry_proxy.api.settings", SimpleNamespace(settings_page_enabled=False)):
            self.assertNotIn('href="/settings"', _with_settings_nav(html))
        with patch("retry_proxy.api.settings", SimpleNamespace(settings_page_enabled=True)):
            self.assertIn('href="/settings"', _with_settings_nav(html))

    def test_request_nav_is_stripped_only_when_disabled(self):
        html = '<nav><a href="/requests">请求排查</a><a href="/settings">配置</a></nav>'
        with patch("retry_proxy.api.settings", SimpleNamespace(request_body_logging=False)):
            self.assertNotIn('href="/requests"', _with_request_diagnostics_nav(html))
        with patch("retry_proxy.api.settings", SimpleNamespace(request_body_logging=True)):
            self.assertIn('href="/requests"', _with_request_diagnostics_nav(html))

    def test_settings_page_hides_its_own_entry_but_keeps_requests_entry(self):
        # 配置页自身不显示"配置"，但仍按开关裁剪"请求排查"
        html = '<nav><a href="/key-pools">号池</a><a href="/requests">请求排查</a><a href="/settings">配置</a></nav>'
        with patch("retry_proxy.api.settings", SimpleNamespace(settings_page_enabled=True, request_body_logging=False)):
            rendered = _admin_page_html(html, settings_nav=False)
        self.assertIn('href="/key-pools"', rendered)
        self.assertNotIn('href="/requests"', rendered)
        self.assertIn('href="/settings"', rendered)

    def test_asset_references_can_be_stripped_for_plain_pages(self):
        html = ('<head><link rel="stylesheet" href="/admin/assets/admin_theme.css"></head>'
                '<body><script src="/admin/assets/admin_ui.js" defer></script></body>')
        stripped = _with_admin_assets(html)
        self.assertNotIn("admin_theme.css", stripped)
        self.assertNotIn("admin_ui.js", stripped)


class AdminNavMarkupTests(unittest.TestCase):
    def test_every_page_marks_the_active_nav_entry(self):
        expected = {
            "stats.html": "/stats",
            "logs.html": "/logs",
            "key_pool.html": "/key-pools",
            "settings.html": "/settings",
            "requests.html": "/requests",
        }
        for name, href in expected.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertEqual(
                text.count('class="active" href="%s" aria-current="page"' % href), 1, name)

    def test_settings_page_links_to_request_diagnostics(self):
        text = (ROOT / "settings.html").read_text(encoding="utf-8")
        self.assertIn('<a href="/requests">请求排查</a>', text)


class DockerPackagingTests(unittest.TestCase):
    def test_shared_assets_are_copied_into_image(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copied = {line.split()[1] for line in dockerfile.splitlines() if line.startswith("COPY ")}
        for asset in ("admin_theme.css", "admin_ui.js"):
            self.assertIn(asset, copied, f"Dockerfile 缺少 COPY {asset}")


import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "fixtures" / "admin_prefs_harness.js"


@unittest.skipUnless(shutil.which("node"), "Node.js required for UI tests")
class AdminPrefsBehaviourTests(unittest.TestCase):
    def test_theme_and_density_toggle_and_persist(self):
        script = HARNESS.read_text(encoding="utf-8").replace(
            "const scriptPath = process.argv[2] || 'admin_ui.js';",
            f"const scriptPath = {str(ROOT / 'admin_ui.js')!r};",
        )
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prefs harness OK", result.stdout)



if __name__ == "__main__":
    unittest.main()
