# llm-retry-proxy

一个面向 LLM API 的本地反向代理转发工具。它对客户端透明透传请求、响应、SSE 流式响应和 Responses WebSocket 双向消息，在上游临时过载或限流时自动重试；也支持多上游路由、号池故障转移、JSONL 日志和可视化分析。

## TL;DR

### 给讯飞 / Coding Plan 用户

把本项目当作一个本地“自动重试层”：配置 `UPSTREAM_URL`（默认就是讯飞 Coding Plan 地址），启动代理后，只需将客户端的 API 地址从上游地址改为 `http://127.0.0.1:8080`，鉴权、请求路径和请求体保持不变。上游临时返回 `503`、`429` 等错误时，代理会自动等待重试，成功后再把结果返回；SSE 流式响应也会透传。

### 给中转站 / 多 Key 用户

把本项目部署在中转站入口，使用 `KEY_POOL_FILE=key_pool.csv` 配置按优先级排列的多个 API key。请求遇到 `429`、`5xx`（或开启 `RETRY_BROAD` 后的鉴权/网络错误）时，代理会冷却当前 key 并自动切换到下一个可用 key，同时记录每次尝试、供应商、模型和 key 标签，便于在 `stats.html` / `logs.html` 中查看。未配置号池时，它仍可作为普通的单上游重试代理使用。

> **号池（Key Pool）** 用于中转站多 key 故障转移：按配置顺序从上到下依次使用，遇到 429/5xx 自动冷却切到下一个可用 key。与重试引擎松耦合，不配置时完全不介入请求流程。

**本项目仅推荐使用串行轮询请求，请慎用竞速模式，不当使用会为模型供应商的服务端点带来极大压力，严重可能会导致您被封号或造成其他经济损失！竞速模式未经过人工测试，开发者不对该项功能的完整性做任何保证！**

## 特性

- 通用反向代理：透传所有路径、Header、Body、Query
- 支持 SSE 流式响应和 Responses WebSocket 双向透传；HTTP 重试只发生在首字节之前
- 503/502/504/529/429 自动重试，支持固定间隔、指数退避和 `Retry-After`
- 默认有最大重试次数保护，可设置为无限重试
- 响应头附带 `X-Forward-Attempts`，告知客户端本次请求尝试次数
- 可选竞速模式、多上游路由和号池多 key 降级
- 按天 JSONL 明细日志、累计汇总和内置可视化分析面板

## 快速开始

### 一键脚本

三端脚本功能一致：自动创建虚拟环境 `.venv`、安装依赖、首次交互式生成 `.env` 并启动服务。首次运行会引导配置：

```
上游地址 [https://maas-coding-api.cn-huabei-1.xf-yun.com/v2]:
供应商标签，如 xfyun [xfyun]:
监听端口 [8080]:
重试间隔秒数 [1.0]:
最大重试次数 (0=无限重试直到成功) [60]:
```

| 平台 | 脚本 | 运行方式 |
|---|---|---|
| Windows (CMD) | `setup.bat` | 双击，或命令行执行 |
| Windows (PowerShell) | `setup.ps1` | `powershell -ExecutionPolicy Bypass -File setup.ps1` |
| Linux / macOS | `setup.sh` | `bash setup.sh`（或 `chmod +x setup.sh && ./setup.sh`） |

> PowerShell 若被执行策略拦截，加 `-ExecutionPolicy Bypass` 参数运行即可，仅对本次生效，不改动系统策略。回车即采用方括号内默认值，后续再次运行会跳过配置直接启动。

### 手动

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 修改 UPSTREAM_URL、LISTEN_PORT 等
python main.py
```

假设上游是 `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`，本代理监听 `8080`：

```bash
# 原本调用
curl https://maas-coding-api.cn-huabei-1.xf-yun.com/v2/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" -d @req.json

# 改为调用本地代理（只换 host，路径/鉴权不变）
curl http://127.0.0.1:8080/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" -d @req.json
```

### Docker Compose

```bash
cp .env.example .env
# 编辑 .env
docker compose up -d --build
```

## 常用配置

最常调整的是：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UPSTREAM_URL` | `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2` | 上游地址，不要带尾斜杠 |
| `LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `LISTEN_PORT` | `8080` | 监听端口 |
| `MAX_RETRIES` | `60` | 最大重试次数；`0` 表示无限重试 |
| `RETRY_INTERVAL` | `1.0` | 非 429 错误的重试间隔/退避基数 |
| `RETRY_INTERVAL_429` | `5.0` | 429 专用重试间隔/退避基数 |
| `RETRY_BROAD` | `off` | 是否把鉴权和网络错误也纳入重试/换 key |
| `HEDGE_MODE` | `off` | `off` 串行；`race` / `stagger` 竞速 |
| `KEY_POOL_FILE` | 空 | CSV 号池文件；优先于 `KEY_POOLS` |
| `ADMIN_PASSWORD` | 空 | `/stats`、`/logs` 和号池管理页的密码 |
| `LOG_DIR` | `logs` | 日志目录 |

完整配置表和默认值见[配置项](docs/configuration.md)。

Codex 桌面端使用自定义 `model_provider` 指向本代理时，可以为 Responses API 开启 WebSocket：

```toml
[model_providers.local]
base_url = "http://127.0.0.1:8080/v1"
wire_api = "responses"
supports_websockets = true
```

代理会把 `http://` / `https://` 上游自动转换为对应的 `ws://` / `wss://`，透传 Responses WebSocket 帧，并在握手失败或连接中断时由 Codex 客户端执行其内置的 HTTP/SSE 回退。

## 文档导航

- [完整配置项](docs/configuration.md)
- [多上游路由](docs/routing.md)
- [号池与在线同步](docs/key-pool.md)
- [重试与竞速模式](docs/retry.md)
- [日志、DLP 与记录格式](docs/logging-and-dlp.md)
- [健康检查与可视化面板](docs/dashboard.md)

## 健康检查

```bash
curl http://127.0.0.1:8080/health
```

`/health` 返回最小探活状态、已配置路由和号池状态，不需要鉴权。管理页面和数据接口说明见[健康检查与可视化面板](docs/dashboard.md)。

## 推荐

💡 [OpenCode](https://opencode.ai) — 本项目辅助开发工具，[使用邀请链接注册](https://opencode.ai/go?ref=RZ04W6NJYV) 双方各获 $5 额度

🚀 [方舟 Coding Plan](https://volcengine.com/L/3H9VZa1bq1s/) — 支持 GLM-5.2、Kimi-K2.7、MiniMax-M3、DeepSeek-V4、Doubao-Seed-2.0 等模型，订阅叠加 9.5 折低至 9.4 元，邀请码：`EMXDHE8B`

🧩 [智谱 Coding Plan](https://www.bigmodel.cn/glm-coding?ic=DPYG6NTSNI) — 国内顶流编程大模型，20+ 主流工具全适配，性价比拉满（笑死，根本抢不到）

🌐 [Nube.sh](https://nube.sh/invite/660603280ZQ7QF) — 高性价比且强劲的弹性云服务器，基于 Zen 3 EPYC，1 vCPU + 1 GB DDR4 每月仅 $1.09 起

## 致谢

- [FastAPI](https://fastapi.tiangolo.com/) — 后端 Web 框架
- [uvicorn](https://www.uvicorn.org/) — ASGI 服务器
- [httpx](https://www.python-httpx.org/) — 异步 HTTP 客户端
- [ECharts](https://echarts.apache.org/) — 统计面板图表库
- 讯飞星辰 MaaS — 上游 LLM 服务

## 免责声明

本项目仅供个人学习与技术研究使用。

- 本项目为本地反向代理转发工具，不对任何上游服务的稳定性、可用性负责
- 使用者应自行确保遵守上游服务（如 LLM API 提供方）的服务条款与使用限制
- 不得用于绕过上游限流、计费或访问控制等用途
- 本项目不向用户收取任何费用
- 使用者应遵守相关平台规则与当地法律法规

## License

[MIT](LICENSE)
