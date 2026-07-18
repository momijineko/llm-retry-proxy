# Repository Guidelines

## 代理工作原则

本文件适用于整个仓库。开始修改前，先阅读 `README.md`、相关 `docs/` 文档和目标模块；不要根据文件名猜测行为。保持改动聚焦，只处理用户要求的范围。保留工作区中已有的用户改动，不做无关重构、格式化或依赖升级。除非用户明确要求，不要创建提交、切换分支或修改运行数据。

## 项目结构

`main.py` 是启动入口，核心 Python 代码位于 `retry_proxy/`：

- `application.py`、`api.py`：FastAPI 生命周期、管理端点和代理入口。
- `retry.py`、`routes.py`：重试、竞速和上游路由。
- `key_pool.py`、`pool_sync.py`、`sync_adapters/`：号池及在线同步。
- `config.py`、`dlp.py`、`log_store.py`、`stats.py`：配置、安全过滤、日志和统计。

根目录的 `stats.html`、`logs.html`、`key_pool.html` 是内嵌管理页面。测试位于 `tests/`，专题文档位于 `docs/`。新增代码应放入现有职责最接近的模块，避免创建只包装一层的抽象。

## 开发与验证

使用 Python 3.10+ 和仓库虚拟环境：

```bash
.venv/bin/python main.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_auth -v
docker compose up -d --build
curl http://127.0.0.1:8080/health
```

修改行为时必须补充或更新回归测试。测试使用 `unittest`、`IsolatedAsyncioTestCase`、`AsyncMock` 和 `patch`；文件命名为 `test_<行为>.py`，方法命名为 `test_<预期结果>`。优先运行目标测试，完成前运行完整套件。纯文档改动可不运行测试，但需检查链接、命令和示例。

## 编码约定

Python 使用四空格缩进和 PEP 8 命名：函数与模块使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。保持现有 FastAPI/httpx 异步模式和包内相对导入。导入按标准库、第三方、本地模块分组。仓库未配置格式化或 lint 工具，以相邻代码为准。注释只解释非显然的约束，不复述代码。

## 安全与配置

不得读取、输出或提交 `.env`、`key_pool.csv`、API Key、账号凭据及 `logs/` 中的敏感数据。新增配置时同步更新 `.env.example` 和对应文档。修改代理、日志、DLP 或管理端点时，必须检查鉴权、脱敏、流式响应、取消传播和超时行为。禁止在测试或示例中写入真实凭据。

## Git 规范

仅在用户明确要求时提交，并且只暂存本次任务涉及的文件。提交格式为：

```text
<type>(<scope>): <中文摘要>

<中文正文，可选；说明动机、行为变化及配置或兼容性影响>
```

`scope` 可省略。优先使用 `feat`、`fix`、`docs`、`build`，按需使用 `test`、`refactor`、`chore`。摘要和正文必须使用中文，例如 `fix: 避免号池备份文件泄漏`。提交前检查 `git diff` 和 `git status`，不得混入用户的其他改动。
