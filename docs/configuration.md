# 配置项

[返回 README](../README.md)

所有配置通过 `.env` 或环境变量设置。可直接复制项目中的 `.env.example` 作为起点。

## Docker 与运行环境

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志时间 |
| `DOCKER_REGISTRY` | `docker.io` | Docker 仓库域名；国内可改为镜像站域名，如 `docker.m.daocloud.io` |
| `PYTHON_BASE_IMAGE` | `library/python:3.12-slim` | 基础镜像命名空间/镜像名:tag，与 `DOCKER_REGISTRY` 拼接为完整引用 |
| `PIP_INDEX_URL` | 清华 PyPI 镜像 | Docker 构建使用的 Python 包索引 |
| `UVICORN_LOOP` | `auto` | Uvicorn 事件循环；`auto` 在可用时使用 uvloop，旧环境模板覆盖为 `asyncio` |

## 服务与访问控制

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `LISTEN_PORT` | `8080` | 监听端口 |
| `ADMIN_PASSWORD` | 空 | 管理页面密码；未配置时 `/stats*`、`/logs*` 和 `/key-pools` 禁用。兼容旧 `ADMIN_TOKEN` |
| `ADMIN_COOKIE_SECURE` | `false` | HTTPS 部署时设为 `true`，限制登录 Cookie 仅通过 HTTPS 发送 |
| `SETTINGS_PAGE_ENABLED` | `false` | 是否启用配置中心页面（/settings）；关闭时页面与导航入口不展示 |
| `PROXY_API_KEY` | 空 | 下游使用号池的凭据；未携带或不匹配时仅作普通透传 |
| `IP_BLACKLIST` | 空 | 拒绝访问的客户端 IP 或 CIDR，多个值用逗号分隔；同时覆盖 HTTP 和 WebSocket |
| `TRUSTED_PROXY_IPS` | `127.0.0.0/8,::1,172.16.0.0/12` | 可信反向代理的直连 IP 或 CIDR；默认覆盖本机及 Docker 172.x 网络 |
| `IP_AUTO_BAN_THRESHOLD` | `20` | 滑动窗口内访问不同路径达到该数量时动态封禁；`0` = 关闭 |
| `IP_AUTO_BAN_WINDOW` | `10` | 动态封禁检测窗口（秒） |
| `IP_AUTO_BAN_DURATION` | `0` | 动态封禁持续时间（秒）；`0` = 永久封禁 |
| `IP_AUTO_BAN_EXEMPT` | `127.0.0.0/8,::1` | 不参与动态封禁的 IP/CIDR；不覆盖静态黑名单 |
| `IP_BAN_STATE_FILE` | `LOG_DIR/.ip_bans.json` | 动态封禁状态文件，保证重启后封禁继续有效 |
| `PROVIDER_ALIASES` | 空 | 统计 provider 显示别名，格式为 `from:to,from:to`；不会改变实际路由 |

例如，直接封禁单个扫描来源及一个网段：

```dotenv
IP_BLACKLIST=152.32.129.213,198.51.100.0/24
```

黑名单在路由匹配和读取请求体之前执行，命中后 HTTP 返回 `403`，WebSocket
以策略违规代码 `1008` 关闭，不会访问上游，也不会为每次被拒请求写日志。
动态封禁默认检测同一 IP 在 10 秒内访问 20 个不同路径的 URL 扫描并永久封禁；重复请求
同一路径不累计。触发时只写一条不含请求路径的动态封禁审计日志，之后的请求
保持静默。将 `IP_AUTO_BAN_DURATION` 设置为正数可改为到期自动解除。状态文件仅保存
IP 和到期时间（永久封禁使用 `0`），不保存扫描路径。

默认可信范围适用于本机 Nginx/Caddy 以及 Docker 172.x 代理网络。若服务不经过
反向代理直接暴露公网，可将 `TRUSTED_PROXY_IPS` 设为空；使用其它容器网段或 CDN
时，应改为实际反向代理的地址，
并确保代理覆盖而不是原样保留来自公网请求的 `CF-Connecting-IP`、
`X-Forwarded-For` 和 `X-Real-IP`。应用会从可信代理链右侧开始跳过可信地址，
使用第一个非可信地址作为客户端 IP。配置变更后需要重启服务。

## 上游、路由与网络

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UPSTREAM_URL` | `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2` | 默认上游地址，不要带尾斜杠 |
| `PROVIDER` | `xfyun` | 供应商标签，写入日志与统计记录 |
| `EXTRA_UPSTREAMS` | 空 | 额外上游路由，格式 `prefix\|url\|provider`，多组用逗号分隔。详见[多上游路由](routing.md) |
| `TRUST_ENV` | `false` | 是否读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等系统代理变量 |

## 连接与响应超时

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CONNECT_TIMEOUT` | `10` | 连接上游超时（秒） |
| `TIMEOUT` | `300` | 普通读写超时（秒）；流式响应中表示相邻两次数据之间的最大间隔 |
| `MAX_REQUEST_BODY` | `67108864` | 请求体最大字节数；超出返回 413，防止超大上传耗尽内存 |
| `RESPONSES_HEADER_TIMEOUT` | `120` | Responses API 整笔请求从开始处理到收到响应头的硬上限（秒）；预算内正常重试，`0` = 不限制 |
| `RESPONSES_ATTEMPT_HEADER_TIMEOUT` | `15` | 流式 Responses 号池请求中单个 key 等待响应头的上限（秒）；超时后取消该次请求并仅在本请求内换 key，不写入全局熔断，`0` = 不限制 |
| `RESPONSES_ATTEMPT_HEADER_TIMEOUT_BODY_LIMIT` | `1048576` | 仅对请求体不超过此字节数的请求启用单 key 短超时；更大的请求使用整笔响应头预算，避免上传和解析耗时被误判为 key 故障；`0` = 不限大小 |


## Codex Responses WebSocket 桥接 (SSE2WS)

> **双向桥接，上游无需支持 WebSocket。** 名称 `sse2ws` 指**响应方向**（上游 SSE → 客户端 WS）：
>
> - **`WS → SSE`（请求方向，客户端 → 上游）**：客户端经 WebSocket 发送 `response.create` 文本帧，桥接逐轮转成上游 HTTP/SSE Responses 请求。
> - **`SSE → WS`（响应方向，上游 → 客户端）**：上游 SSE 事件流逐帧转成 WebSocket JSON 文本帧推回客户端，连接内可连续多轮。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SSE2WS_MODE` | `off` | 设为 `bridge` 开启。关闭时不注册 WS 路由，客户端自动回退到 HTTP/SSE |
| `SSE2WS_FIRST_EVENT_TIMEOUT` | `30` | 上游已返回响应头但迟迟不发首事件时判定该次尝试失败的超时（秒） |
| `SSE2WS_FIRST_EVENT_RETRIES` | `2` | 首事件超时后整轮重试的次数；`0` = 不重试 |
| `SSE2WS_FIRST_MESSAGE_TIMEOUT` | `30` | 连接建立后等待客户端第一条 `response.create` 的超时（秒） |
| `SSE2WS_INTER_TURN_IDLE_TIMEOUT` | `300` | 轮与轮之间（等待下一条 `response.create`）的空闲超时（秒）；超时关闭连接 |
| `SSE2WS_MAX_BODY_BYTES` | `67108864` | 单轮 `response.create` 载荷上限（字节），超出返回错误 |

行为说明：

- 多轮上下文通过连接内累积 + 完整 input 重放实现：续轮（携带 `previous_response_id` 或 `function_call_output`）时丢弃 `previous_response_id`，把累积的上下文 item 与当前 input 合并后发给上游，适配无状态的 HTTP/SSE 上游。
- 终止事件以 `response.completed` / `response.incomplete` / `response.failed` / `response.cancelled` / `error` 为准；提前 EOF 视为失败并下发 error 帧，不当作成功。
- 鉴权、号池路由、重试、熔断与日志记录与普通 HTTP 请求共用同一套引擎；日志 `method` 标记为 `WS`。


## 重试与退避

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_RETRIES` | `60` | 单个下游请求在响应头之前的实际上游尝试总上限；`0` = 无限重试。桥接模式与纯 SSE 共用此预算 |
| `RETRY_STATUS_CODES` | `503,502,504,524,529,429` | 触发重试的上游状态码，逗号分隔 |
| `RETRY_BROAD` | `off` | 开启后，5xx、429、401/403 和网络异常均触发重试或换 key；普通 JSON 400、404、422 等请求错误仍直接透传。号池请求的 HTML 400 会作为网关/CDN 故障切换分组 |
| `RETRY_INTERVAL` | `1.0` | 非 429 错误的基础重试间隔（秒） |
| `RETRY_BACKOFF` | `false` | 是否对非 429 错误启用指数退避和抖动 |
| `RETRY_BACKOFF_MAX` | `60` | 非 429 指数退避上限（秒） |
| `RETRY_INTERVAL_429` | `5.0` | 429 基础重试间隔（秒）；优先尊重上游 `Retry-After` |
| `RETRY_BACKOFF_429` | `true` | 是否对连续 429 启用指数退避和抖动 |
| `RETRY_BACKOFF_MAX_429` | `60` | 429 指数退避上限（秒） |
| `RETRY_AFTER_MAX` | `0` | 上游 `Retry-After` 头封顶值（秒）；`0` 表示不封顶，完全尊重上游。非 0 时重试等待与 key 熔断均不超过该值，用于抵御异常巨大的 `Retry-After` |

## 竞速模式

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HEDGE_MODE` | `off` | `off` = 串行重试，`race` = 每轮并发竞速，`stagger` = 交错补发 |
| `MAX_CONCURRENT` | `10` | 竞速模式下的最大并发数 |

## 号池来源与鉴权

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KEY_POOL_FILE` | 空 | 号池 CSV 文件，优先于 `KEY_POOLS`。详见[号池与在线同步](key-pool.md) |
| `KEY_POOLS` | 空 | 环境变量形式的号池配置。详见[号池与在线同步](key-pool.md) |
| `KEY_AUTH_HEADER` | `authorization` | 注入上游 key 使用的 Header 名 |
| `KEY_AUTH_SCHEME` | `Bearer` | 鉴权 scheme；设为空时只发送裸 key |

## 号池熔断与选择

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KEY_COOLDOWN` | `30` | 旧版兼容值；未配置 `KEY_COOLDOWN_5XX` 时作为默认冷却时间 |
| `KEY_COOLDOWN_5XX` | `30` | key 遇到 5xx 或非主机级传输异常后的基础熔断时间（秒） |
| `KEY_COOLDOWN_429` | `60` | key 遇到 429 后的基础熔断时间（秒）；更长的 `Retry-After` 优先 |
| `KEY_COOLDOWN_AUTH` | `1800` | key 遇到 401/403 后的基础熔断时间（秒） |
| `KEY_COOLDOWN_MAX` | `3600` | 连续同类错误指数熔断的上限（秒），不截短 `Retry-After` |
| `KEY_COOLDOWN_BACKOFF` | `true` | 同类错误连续发生时按 `1→2→4...` 倍延长熔断；成功后清零 |
| `KEY_STICKY` | `120` | 成功 key 的分组粘性时间（秒）；“兼顾两者”使用不续期的固定窗口且窗口内仍允许单分组定时复测，其它策略沿用空闲续期；失败候选不建立窗口，当前 key 熔断时仍立即切换；`0` = 禁用 |
| `KEY_POOL_WAIT_TIMEOUT` | `120` | 所有候选 key 均熔断时的最长等待时间（秒）；超时返回 503，`0` = 不限制 |
| `KEY_TTFT_STALE_AFTER` | `300` | 真实首 Token 样本的有效期，以及复测失败后再次尝试的等待时间（秒） |
| `KEY_TTFT_RETEST_INTERVAL` | `60` | “兼顾两者”轮转复测单个低倍率分组的最小间隔（秒） |
| `KEY_TTFT_CONFIRMATIONS` | `2` | 当前分组升级前要求的连续慢样本数；低倍率恢复探测达到恢复线后立即切换 |
| `KEY_TTFT_HYSTERESIS` | `0.1` | 切换滞回比例；目标 5 秒、值为 0.1 时，超过 5.5 秒升级，低于 4.5 秒降回 |
| `KEY_CACHE_MISS_THRESHOLD` | `3` | 同一 Responses 会话在同一“端点类型 + 模型 + 分组”连续多少次符合条件的响应无缓存后熔断整个分组；`0` = 禁用 |
| `KEY_CACHE_MISS_MIN_INPUT_TOKENS` | `1024` | 计入连续无缓存判定的最小输入 token 数，短请求不会误触发 |
| `KEY_CACHE_MISS_COOLDOWN` | `3600` | 连续无缓存分组的熔断时间（秒）；`0` = 禁用 |

## 在线同步

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KEY_POOL_SYNC_DEFAULT_ADAPTER` | `sub2api` | 管理页新增连接时默认使用的同步适配器 |
| `KEY_POOL_SYNC_URL` | `UPSTREAM_URL` | 管理页新增连接时预填的上游地址 |
| `KEY_POOL_SYNC_INTERVAL` | `300` | 自动同步周期（秒）；`0` = 仅手动同步 |
| `KEY_POOL_CREATE_DELAY` | `1.5` | 批量创建 key 时相邻请求的间隔（秒） |
| `KEY_POOL_SYNC_STATE_FILE` | `LOG_DIR/.key_pool_sync.json` | 同步连接、刷新令牌与最近成功号池的持久化文件 |
| `KEY_POOL_SYNC_SECRET` | `ADMIN_PASSWORD` | 状态文件登录凭据和同步得到的完整上游 Key 的加密主密钥；留空时回退到 `ADMIN_PASSWORD`，两者均未设置则不加密（明文落盘，向后兼容） |

## 上游兼容

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IMAGE_UPSTREAM_USER_AGENT` | 空 | 图片请求转发时覆盖上游 User-Agent |
| `IMAGE_UPSTREAM_ORIGINATOR` | 空 | 图片请求转发时覆盖上游 Originator |

## Token 统计

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TOKEN_STATS_INJECT_USAGE` | `false` | 开启后，为未显式设置 `stream_options.include_usage` 的 OpenAI Chat 流式请求注入该选项；默认关闭以保持请求体透明透传，不支持 `stream_options` 的兼容上游应保持关闭 |

代理会从 OpenAI Chat/Embeddings/Responses、Anthropic Messages 和 Gemini
响应中提取输入、输出、总量及缓存读取 token。非流式响应直接读取 JSON；SSE
响应在流结束后写入日志。上游未返回 usage 时，对应日志不写 token 字段。

## 日志

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOG_DIR` | `logs` | 日志目录；明细按天拆分，累计汇总存 `_summary.json` |
| `LOG_RETENTION_DAYS` | `30` | 明细日志保留天数；`0` = 不清理，累计汇总不受影响 |
| `LOG_LEVEL` | `INFO` | 控制台日志级别 |
| `LOG_FILE` | `retry_log.jsonl` | 旧版单文件日志路径，仅用于自动迁移 |

## 请求正文敏感信息防护

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DLP_MODE` | `off` | `off` = 关闭，`audit` = 仅告警，`redact` = 脱敏后转发，`block` = 拦截 |
| `DLP_RULES` | 见 `.env.example` | 启用的 DLP 规则，逗号分隔 |
| `DLP_RULE_FILE` | `retry_proxy/dlp_rules.yaml` | 自定义 DLP 规则文件，支持 JSON/YAML |
| `DLP_ALLOW_EXEMPTIONS` | `false` | 是否允许正文固定标记跳过检查；不可信客户端应保持关闭 |
| `DLP_EXEMPT_START` | `[[ALLOW_SENSITIVE]]` | 主动豁免区间的起始标记 |
| `DLP_EXEMPT_END` | `[[/ALLOW_SENSITIVE]]` | 主动豁免区间的结束标记 |
| `DLP_STRIP_EXEMPT_MARKERS` | `true` | 转发前是否移除主动豁免标记 |
| `DLP_MAX_BODY_BYTES` | `16777216` | DLP 扫描请求体的字节上限；`redact`/`block` 模式下超限返回 413 |
| `DLP_DECODE_DEPTH` | `2` | Base64/Base64URL、hex、percent 编码递归扫描深度，范围 0～8；`0` = 关闭 |
| `DLP_DECODE_MAX_CANDIDATES` | `100` | 单次请求最多接受的可打印解码候选片段数 |
| `DLP_DECODE_MAX_BYTES` | `1048576` | 单次请求累计处理解码结果的字节数 |
| `DLP_KNOWN_SECRET_MIN_LENGTH` | `8` | 号池 Key 精确匹配的最小长度 |
| `DLP_FAIL_CLOSED` | `false` | DLP 无法解析非空正文时是否返回 422 |

## Docker 部署补充

`compose.yaml` 会将 `LOG_DIR` 映射到宿主机的 `./logs`，并在未设置 `KEY_POOL_FILE` 时将其默认指向容器内的 `/app/key_pool.csv`。部署前请准备 `.env`；需要静态号池时复制 `key_pool.csv.example` 为 `key_pool.csv`。

默认模板 `.env.example` 使用 Docker Hub；国内无代理环境可将完整模板
`.env.cn.example` 复制为 `.env`，其中为 `DOCKER_REGISTRY` 预置了国内镜像站
域名。`DOCKER_REGISTRY` 与 `PYTHON_BASE_IMAGE` 共同组成完整镜像地址。

Linux 旧内核（如 3.10）配合 Docker 19 时，默认 seccomp 规则可能使新版
glibc 的 `clone3` 调用返回 `EPERM`，表现为 `can't start new thread`。仅在确认
遇到该问题后叠加兼容模板：

```bash
docker compose -f compose.yaml -f compose.legacy.yaml up -d --build
```

`compose.legacy.yaml` 会将 `UVICORN_LOOP` 设为 `asyncio`，并通过
`seccomp:unconfined` 关闭容器的系统调用过滤。后者会降低容器隔离强度，因此
不应在现代内核或新版 Docker 环境中使用。

号池、在线同步调度和熔断状态是进程内状态，生产部署必须保持单 Uvicorn worker、单容器副本。当前不支持通过多 worker 或多副本横向扩容；多个进程会各自持有不同的号池，并竞争写入同步状态文件。

## 配置中心页面

默认关闭：配置 `SETTINGS_PAGE_ENABLED=true` 后重启生效。关闭时 `/settings` 页面、`/admin/settings` 接口与三个面板的导航入口均不展示。

配置 `ADMIN_PASSWORD` 后，可访问 `/settings` 在网页中查看和修改全部配置项（与 `.env.example` 全集一致）：

- 每项展示三态：`.env` 文件值、当前生效值与默认值；未写入 `.env` 的项以默认值生效
- 生效类别徽标：
  - **立即生效**：保存后无需重启即对后续请求生效（重试、超时、DLP、号池熔断等运行时参数）
  - **重启后生效**：保存到 `.env`，需重启进程生效（监听地址、路由、SSE2WS 等启动期固化配置）
  - **重建镜像后生效**：构建期配置（`DOCKER_REGISTRY`、`PYTHON_BASE_IMAGE`、`PIP_INDEX_URL`），保存到 `.env` 后执行 `docker compose build` 重建镜像生效
- 敏感项（`ADMIN_PASSWORD`、`ADMIN_TOKEN`、`PROXY_API_KEY`、`KEY_POOLS`、`KEY_POOL_SYNC_SECRET`）不回显明文，输入框留空表示不修改（仅点击"重置"才恢复默认）
- 非敏感项清空输入框或点击"重置"即恢复默认值（从 `.env` 移除对应项）
- 容器部署（`compose.yaml`）已把宿主机 `.env` 挂载到容器内 `/app/.env`：**重启后生效**项保存后直接持久化到宿主机文件，`docker compose up -d` 重建容器后经 `env_file` 重新注入生效
- 若自定义 compose 未挂载 `.env`：**立即生效**项保存后仅内存热更新（重启容器后丢失）；**重启后生效**项保存无效（既不写文件也不改内存），页面保存时会给出对应提示，此时请在宿主机 `.env` 或 compose 环境变量中修改后重建容器

注意：`LISTEN_HOST`、`KEY_POOL_FILE`、`LOG_DIR`、`TZ` 被 `compose.yaml` 的 `environment` 覆盖，修改 `.env` 对这四个键无效，需直接改 compose 文件。
