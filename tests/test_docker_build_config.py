import ast
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "compose.yaml"
LEGACY_COMPOSE_FILE = REPO_ROOT / "compose.legacy.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CN_ENV_EXAMPLE = REPO_ROOT / ".env.cn.example"
MAIN_FILE = REPO_ROOT / "main.py"

# Dockerfile 和默认模板走 Docker Hub，国内模板使用镜像站。
EXPECTED_DOCKERFILE_REFERENCE = "docker.io/library/python:3.12-slim"
EXPECTED_ENV_REFERENCE = "docker.io/library/python:3.12-slim"
EXPECTED_CN_ENV_REFERENCE = "docker.m.daocloud.io/library/python:3.12-slim"


def _arg_defaults(text):
    """提取 Dockerfile 中 `ARG NAME=VALUE` 的默认值映射。"""
    defaults = {}
    for m in re.finditer(r"^ARG\s+(\w+)=(\S+)\s*$", text, re.MULTILINE):
        defaults[m.group(1)] = m.group(2)
    return defaults


def _env_values(text):
    """提取环境模板中的 `NAME=VALUE` 赋值（跳过注释与空行）。"""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def _is_valid_reference(ref):
    """粗略校验拼接结果是合法 Docker 镜像引用：无 scheme、无连续斜杠、含域名/路径/tag。"""
    if "://" in ref or "//" in ref:
        return False
    return re.match(r"^[^/\s]+(?::\d+)?(?:/[^/\s]+)+(?::[^\s/]+)?$", ref) is not None


class DockerBuildConfigTests(unittest.TestCase):
    def setUp(self):
        self.arg_defaults = _arg_defaults(DOCKERFILE.read_text(encoding="utf-8"))
        # 仓库文件均为 UTF-8；显式指定编码，避免 Windows 区域默认 GBK 解析失败
        self.env = _env_values(ENV_EXAMPLE.read_text(encoding="utf-8"))
        self.cn_env = _env_values(CN_ENV_EXAMPLE.read_text(encoding="utf-8"))
        self.compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        self.legacy_compose = yaml.safe_load(LEGACY_COMPOSE_FILE.read_text(encoding="utf-8"))

    def test_dockerfile_defaults_produce_valid_reference(self):
        ref = f"{self.arg_defaults['DOCKER_REGISTRY']}/{self.arg_defaults['PYTHON_BASE_IMAGE']}"
        self.assertTrue(_is_valid_reference(ref), f"非法镜像引用: {ref}")
        self.assertEqual(ref, EXPECTED_DOCKERFILE_REFERENCE)

    def test_env_example_produces_expected_reference(self):
        ref = f"{self.env['DOCKER_REGISTRY']}/{self.env['PYTHON_BASE_IMAGE']}"
        self.assertTrue(_is_valid_reference(ref), f"非法镜像引用: {ref}")
        self.assertEqual(ref, EXPECTED_ENV_REFERENCE)

    def test_cn_env_example_produces_expected_reference(self):
        ref = f"{self.cn_env['DOCKER_REGISTRY']}/{self.cn_env['PYTHON_BASE_IMAGE']}"
        self.assertTrue(_is_valid_reference(ref), f"非法镜像引用: {ref}")
        self.assertEqual(ref, EXPECTED_CN_ENV_REFERENCE)

    def test_env_examples_differ_only_by_registry(self):
        default_values = dict(self.env)
        cn_values = dict(self.cn_env)
        default_values.pop("DOCKER_REGISTRY")
        cn_values.pop("DOCKER_REGISTRY")
        self.assertEqual(default_values, cn_values)

    def test_docker_registry_is_bare_domain(self):
        # DOCKER_REGISTRY 必须是纯域名，不含路径分隔符；library 命名空间误放入此
        # 处会破坏换站时的复用，且与 PYTHON_BASE_IMAGE 的命名空间职责混淆。
        for source, values in (("arg_defaults", self.arg_defaults), ("env", self.env),
                               ("cn_env", self.cn_env)):
            value = values["DOCKER_REGISTRY"]
            self.assertNotIn("/", value, f"DOCKER_REGISTRY({source}) 应为纯域名: {value}")

    def test_python_base_image_includes_namespace(self):
        # PYTHON_BASE_IMAGE 必须含命名空间段（如 library/），否则与纯域名拼接会缺命名空间。
        for source, values in (("arg_defaults", self.arg_defaults), ("env", self.env),
                               ("cn_env", self.cn_env)):
            value = values["PYTHON_BASE_IMAGE"]
            self.assertIn("/", value, f"PYTHON_BASE_IMAGE({source}) 应含命名空间: {value}")

    def test_image_values_carry_no_scheme(self):
        # Docker 镜像引用语法不支持 http(s):// 前缀，Docker 默认以 HTTPS 拉取。
        for values in (self.arg_defaults, self.env, self.cn_env):
            for key in ("DOCKER_REGISTRY", "PYTHON_BASE_IMAGE"):
                self.assertNotIn("://", values[key], f"{key} 镜像引用不支持 scheme: {values[key]}")

    def test_default_compose_keeps_seccomp_enabled(self):
        service = self.compose["services"]["llm-retry-proxy"]
        self.assertNotIn("security_opt", service)

    def test_legacy_compose_explicitly_enables_compatibility_options(self):
        service = self.legacy_compose["services"]["llm-retry-proxy"]
        self.assertEqual(service["security_opt"], ["seccomp:unconfined"])
        self.assertEqual(service["environment"]["UVICORN_LOOP"], "asyncio")

    def test_default_event_loop_remains_auto(self):
        self.assertEqual(self.env["UVICORN_LOOP"], "auto")
        self.assertEqual(self.cn_env["UVICORN_LOOP"], "auto")

        tree = ast.parse(MAIN_FILE.read_text(encoding="utf-8"))
        loop_values = [
            keyword.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "uvicorn"
            and node.func.attr == "run"
            for keyword in node.keywords
            if keyword.arg == "loop"
        ]
        self.assertEqual(len(loop_values), 1)
        self.assertEqual(ast.unparse(loop_values[0]), "settings.uvicorn_loop")

    def test_all_admin_pages_are_copied_into_image(self):
        # 管理页面必须全部 COPY 进镜像，新增页面忘记打包时访问会返回 not found
        copied = {line.split()[1] for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
                  if line.startswith("COPY ")}
        for page in ("stats.html", "logs.html", "key_pool.html", "settings.html"):
            self.assertIn(page, copied, f"Dockerfile 缺少 COPY {page}")

    def test_compose_mounts_env_file_for_settings_persistence(self):
        # 配置中心持久化依赖宿主机 .env 挂载：未挂载时"重启后生效"项保存无效
        volumes = self.compose["services"]["llm-retry-proxy"]["volumes"]
        self.assertTrue(
            any("/app/.env" in v and v.startswith("./.env") for v in volumes),
            f"compose.yaml volumes 缺少 ./.env:/app/.env 挂载: {volumes}",
        )


if __name__ == "__main__":
    unittest.main()
