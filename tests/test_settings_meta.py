"""配置元数据表一致性测试：防止新增配置项未登记进配置中心页面"""

import re
import unittest
from pathlib import Path

from retry_proxy.settings_meta import CONFIG_ITEMS, CONFIG_ITEMS_BY_KEY, HOT, REBUILD, RESTART

ROOT = Path(__file__).resolve().parents[1]

# config.py 未读取、但由 compose 构建期 / stats.py 消费的键
EXTERNAL_KEYS = {
    "DOCKER_REGISTRY",
    "PIP_INDEX_URL",
    "PROVIDER_ALIASES",
    "PYTHON_BASE_IMAGE",
    "TZ",
}

SECRET_KEYS = {
    "ADMIN_PASSWORD",
    "ADMIN_TOKEN",
    "PROXY_API_KEY",
    "KEY_POOLS",
    "KEY_POOL_SYNC_SECRET",
}


def _config_py_keys() -> set:
    text = (ROOT / "retry_proxy" / "config.py").read_text(encoding="utf-8")
    keys = set()
    keys.update(re.findall(r'os\.getenv\(\s*"(.*?)"', text, re.DOTALL))
    keys.update(re.findall(r'_bool\(\s*"(.*?)"', text, re.DOTALL))
    return keys


def _config_py_literal_defaults() -> dict:
    """提取 config.py 中 os.getenv/_bool 的双引号字面默认值

    哨兵/表达式默认（嵌套 os.getenv、str(64*1024*1024)、os.path.join 等）
    不在提取之列，由默认值一致性测试单独锁定
    """
    text = (ROOT / "retry_proxy" / "config.py").read_text(encoding="utf-8")
    defaults = {}
    for key, value in re.findall(r'os\.getenv\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*"([^"]*)"', text):
        defaults.setdefault(key, value)
    for key, value in re.findall(r'_bool\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*"([^"]*)"', text):
        defaults.setdefault(key, value)
    return defaults


def _example_keys() -> set:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set()
    for line in text.splitlines():
        m = re.match(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]*)\s*=.*", line)
        if m:
            keys.add(m.group(1))
    return keys


class SettingsMetaTests(unittest.TestCase):
    def test_meta_covers_all_program_config_keys(self):
        # 程序读取的键（config.py + 外部消费键）必须全部登记在元数据中
        expected = _config_py_keys() | EXTERNAL_KEYS
        self.assertEqual(
            set(CONFIG_ITEMS_BY_KEY) - expected,
            set(),
            "元数据中存在程序未读取的键",
        )
        self.assertEqual(
            expected - set(CONFIG_ITEMS_BY_KEY),
            set(),
            "新增配置项未登记进 settings_meta.py",
        )

    def test_meta_matches_env_example_full_set(self):
        # 配置页面以 .env.example 全集为口径
        self.assertEqual(set(CONFIG_ITEMS_BY_KEY), _example_keys())

    def test_keys_are_unique(self):
        self.assertEqual(len(CONFIG_ITEMS), len(CONFIG_ITEMS_BY_KEY))

    def test_apply_category_is_valid(self):
        for item in CONFIG_ITEMS:
            self.assertIn(item.apply, (HOT, RESTART, REBUILD), item.key)

    def test_group_is_registered(self):
        from retry_proxy.settings_meta import GROUPS

        for item in CONFIG_ITEMS:
            self.assertIn(item.group, GROUPS, item.key)

    def test_group_order_matches_side_nav(self):
        # 卡片渲染顺序与页面侧栏导航（运行/请求/号池/防护）展开顺序一致
        from retry_proxy.settings_meta import GROUPS

        nav_order = [
            # 运行
            "Docker 与运行环境", "服务与访问控制", "上游、路由与网络", "日志",
            # 请求
            "Codex Responses WebSocket 桥接 (SSE2WS)", "连接与响应超时",
            "重试与退避", "竞速模式",
            # 号池
            "号池来源与鉴权", "号池熔断与选择", "在线同步",
            # 防护
            "Token 统计", "上游兼容", "请求正文敏感信息防护",
        ]
        self.assertEqual(list(GROUPS), nav_order)

    def test_tz_belongs_to_docker_group(self):
        # TZ 属于 Docker 与运行环境分组（与 .env.example 段落一致）
        self.assertEqual(CONFIG_ITEMS_BY_KEY["TZ"].group, "Docker 与运行环境")

    def test_time_items_declare_units(self):
        # 时间类配置项必须声明单位（秒/天），非时间数值项不得误标
        seconds = {item.key for item in CONFIG_ITEMS if item.unit == "秒"}
        days = {item.key for item in CONFIG_ITEMS if item.unit == "天"}
        self.assertIn("RETRY_INTERVAL", seconds)
        self.assertIn("KEY_COOLDOWN_5XX", seconds)
        self.assertIn("SSE2WS_FIRST_EVENT_TIMEOUT", seconds)
        self.assertEqual(days, {"LOG_RETENTION_DAYS"})
        self.assertNotIn("MAX_REQUEST_BODY", seconds)
        self.assertNotIn("KEY_TTFT_CONFIRMATIONS", seconds)
        self.assertNotIn("KEY_CACHE_HIT_CONFIRMATIONS", seconds)
        self.assertNotIn("LISTEN_PORT", seconds)

    def test_tz_is_fourth_card_in_docker_group(self):
        # Docker 组卡片顺序：三个构建期配置在前，容器时区排第四（两行两列布局）
        docker_keys = [item.key for item in CONFIG_ITEMS if item.group == "Docker 与运行环境"]
        self.assertEqual(docker_keys, ["DOCKER_REGISTRY", "PYTHON_BASE_IMAGE", "PIP_INDEX_URL", "TZ"])

    def test_settings_page_enabled_metadata(self):
        # 配置页面开关：默认关闭、服务与访问控制分组、重启后生效，且紧随管理密码之后
        it = CONFIG_ITEMS_BY_KEY["SETTINGS_PAGE_ENABLED"]
        self.assertEqual(it.default, "false")
        self.assertEqual(it.group, "服务与访问控制")
        self.assertEqual(it.apply, RESTART)
        self.assertEqual(it.type, "bool")
        keys = [item.key for item in CONFIG_ITEMS if item.group == "服务与访问控制"]
        self.assertLess(keys.index("ADMIN_PASSWORD"), keys.index("SETTINGS_PAGE_ENABLED"))
        self.assertLess(keys.index("SETTINGS_PAGE_ENABLED"), keys.index("ADMIN_COOKIE_SECURE"))

    def test_api_docs_enabled_metadata(self):
        item = CONFIG_ITEMS_BY_KEY["API_DOCS_ENABLED"]
        self.assertEqual(item.default, "false")
        self.assertEqual(item.group, "服务与访问控制")
        self.assertEqual(item.apply, RESTART)
        self.assertEqual(item.type, "bool")
        keys = [item.key for item in CONFIG_ITEMS if item.group == "服务与访问控制"]
        self.assertLess(keys.index("SETTINGS_PAGE_ENABLED"), keys.index("API_DOCS_ENABLED"))
        self.assertLess(keys.index("API_DOCS_ENABLED"), keys.index("ADMIN_COOKIE_SECURE"))

    def test_ip_ban_keys_metadata(self):
        # IP 访问控制 7 键：分组、生效方式、敏感性与类型登记正确，且顺序位于号池鉴权键之间
        ip_keys = [
            "IP_BLACKLIST",
            "TRUSTED_PROXY_IPS",
            "IP_AUTO_BAN_THRESHOLD",
            "IP_AUTO_BAN_WINDOW",
            "IP_AUTO_BAN_DURATION",
            "IP_AUTO_BAN_EXEMPT",
            "IP_BAN_STATE_FILE",
        ]
        for key in ip_keys:
            self.assertIn(key, CONFIG_ITEMS_BY_KEY, f"{key} 未登记进 settings_meta.py")
            item = CONFIG_ITEMS_BY_KEY[key]
            self.assertEqual(item.group, "服务与访问控制", key)
            self.assertEqual(item.apply, RESTART, key)
            self.assertFalse(item.secret, key)
        self.assertEqual(CONFIG_ITEMS_BY_KEY["IP_AUTO_BAN_WINDOW"].unit, "秒")
        self.assertEqual(CONFIG_ITEMS_BY_KEY["IP_AUTO_BAN_DURATION"].unit, "秒")
        self.assertEqual(CONFIG_ITEMS_BY_KEY["IP_BLACKLIST"].unit, "")
        expected_types = {
            "IP_BLACKLIST": "csv",
            "TRUSTED_PROXY_IPS": "csv",
            "IP_AUTO_BAN_THRESHOLD": "int",
            "IP_AUTO_BAN_WINDOW": "float",
            "IP_AUTO_BAN_DURATION": "float",
            "IP_AUTO_BAN_EXEMPT": "csv",
            "IP_BAN_STATE_FILE": "str",
        }
        for key, expected in expected_types.items():
            self.assertEqual(CONFIG_ITEMS_BY_KEY[key].type, expected, key)
        keys = [item.key for item in CONFIG_ITEMS if item.group == "服务与访问控制"]
        for key in ip_keys:
            self.assertLess(keys.index("PROXY_API_KEY"), keys.index(key), key)
            self.assertLess(keys.index(key), keys.index("PROVIDER_ALIASES"), key)

    def test_secret_keys_are_marked(self):
        marked = {item.key for item in CONFIG_ITEMS if item.secret}
        self.assertEqual(marked, SECRET_KEYS)

    def test_all_items_have_chinese_names(self):
        # 配置页面主标题使用中文名，缺失时页面会回退显示环境变量名
        missing = [item.key for item in CONFIG_ITEMS if not item.name]
        self.assertEqual(missing, [])

    def test_rebuild_items_are_build_only(self):
        rebuild = {item.key for item in CONFIG_ITEMS if item.apply == REBUILD}
        self.assertEqual(rebuild, {"DOCKER_REGISTRY", "PYTHON_BASE_IMAGE", "PIP_INDEX_URL"})

    def test_rebuild_items_are_editable_cards(self):
        # 构建期配置渲染为可编辑卡片（与容器时区组成 Docker 组两行两列），不再作为分组描述隐藏
        hidden = {item.key for item in CONFIG_ITEMS if item.hidden}
        self.assertEqual(hidden, set())

    def test_meta_defaults_match_actual_effective_defaults(self):
        # 页面"默认"列需与实际生效默认一致：config.py 字面默认逐项比对；
        # 哨兵/回退默认（URL 回退 UPSTREAM_URL、状态文件拼接 LOG_DIR、规则文件绝对路径）单独锁定
        literal = _config_py_literal_defaults()
        for key, cfg_default in literal.items():
            item = CONFIG_ITEMS_BY_KEY.get(key)
            if item is None:
                continue
            if key == "KEY_POOL_SYNC_STATE_FILE":
                # config.py 字面默认是空串，实际默认由 LOG_DIR 拼接，meta 以展示默认值为准
                continue
            if key == "IP_BAN_STATE_FILE":
                # config.py 字面默认是空串，实际默认由 LOG_DIR 拼接，meta 以展示默认值为准
                continue
            self.assertEqual(item.default, cfg_default, key)
        self.assertEqual(CONFIG_ITEMS_BY_KEY["KEY_POOL_SYNC_URL"].default, "UPSTREAM_URL")
        self.assertEqual(CONFIG_ITEMS_BY_KEY["KEY_POOL_SYNC_STATE_FILE"].default, "logs/.key_pool_sync.json")
        self.assertEqual(CONFIG_ITEMS_BY_KEY["IP_BAN_STATE_FILE"].default, "logs/.ip_bans.json")
        self.assertEqual(CONFIG_ITEMS_BY_KEY["DLP_RULE_FILE"].default, "retry_proxy/dlp_rules.yaml")

    def test_hot_items_are_read_per_request(self):
        # 热更新项必须是不依赖启动期固化的键：抽检几个代表
        hot = {item.key for item in CONFIG_ITEMS if item.apply == HOT}
        for key in ("RETRY_INTERVAL", "DLP_MODE", "KEY_COOLDOWN_5XX",
                    "PROXY_API_KEY", "MAX_REQUEST_BODY", "PROVIDER_ALIASES"):
            self.assertIn(key, hot, key)
        # 启动期固化的键不得标记为 hot
        for key in ("LISTEN_PORT", "TIMEOUT", "UPSTREAM_URL", "SSE2WS_MODE", "KEY_POOLS"):
            self.assertNotIn(key, hot, key)


if __name__ == "__main__":
    unittest.main()
