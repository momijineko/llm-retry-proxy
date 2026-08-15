"""启动日志横幅测试"""

import unittest
from unittest.mock import patch

from retry_proxy.application import _STARTUP_BANNER, _log_startup


class StartupBannerTests(unittest.TestCase):
    def test_banner_is_logged_line_by_line(self):
        with patch("retry_proxy.application.logger") as mock_logger:
            _log_startup()
        banner_calls = [
            call for call in mock_logger.info.call_args_list
            if "\033[36m" in str(call.args[0])
        ]
        self.assertEqual(len(banner_calls), len(_STARTUP_BANNER.splitlines()))
        for call in banner_calls:
            line = str(call.args[0]).split("\033[36m", 1)[1].split("\033[0m")[0]
            # 行内对齐必须使用 NBSP：普通空格会在 HTML 日志面板中被折叠，
            # 导致 ASCII art 排版错位
            self.assertNotIn(" ", line)
            self.assertIn("\u00a0", line)

    def test_banner_keeps_same_visual_width_as_source(self):
        for source, _ in ((line, None) for line in _STARTUP_BANNER.splitlines()):
            rendered = source.replace(" ", "\u00a0")
            self.assertEqual(len(rendered), len(source))


if __name__ == "__main__":
    unittest.main()
