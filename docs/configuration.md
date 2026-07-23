# 配置项

[返回 README](../README.md)

所有配置通过 `.env` 或环境变量设置。可直接复制项目中的 `.env.example` 作为起点。

## Docker 与运行环境

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志时间 |
| `PIP_INDEX_URL` | 清华 PyPI 镜像 | Docker 构建使用的 Python 包索引 |

## 服务与访问控制

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `LISTEN_PORT` | `8080` | 监听端口 |
| `ADMIN_PASSWORD` | 空 | 管理页面密码；未配置时 `/stats*`、`/logs*` 和 `/key-pools` 禁用。兼容旧 `ADMIN_TOKEN` |
| `ADMIN_COOKIE_SECURE` | `false` | HTTPS 部署时设为 `true`，限制登录 Cookie 仅通过 HTTPS 发送 |
| `PROXY_API_KEY` | 空 | 下游使用号池的凭据；未携带或不匹配时仅作普通透传 |

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
| `RESPONSES_HEADER_TIMEOUT` | `120` | Responses API 整笔请求从开始处理到收到响应头的硬上限（秒）；预算内正常重试，`0` = 不限制 |
| `RESPONSES_ATTEMPT_HEADER_TIMEOUT` | `15` | 流式 Responses 号池请求中单个 key 等待响应头的上限（秒）；超时后取消该次请求、熔断并换 key，`0` = 不限制 |

## 重试与退避

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_RETRIES` | `60` | 最大重试次数；`0` = 无限重试 |
| `RETRY_STATUS_CODES` | `503,502,504,524,529,429` | 触发重试的上游状态码，逗号分隔 |
| `RETRY_BROAD` | `off` | 开启后，5xx、429、401/403 和网络异常均触发重试或换 key；400/404/422 等请求错误仍直接透传 |
| `RETRY_INTERVAL` | `1.0` | 非 429 错误的基础重试间隔（秒） |
| `RETRY_BACKOFF` | `false` | 是否对非 429 错误启用指数退避和抖动 |
| `RETRY_BACKOFF_MAX` | `60` | 非 429 指数退避上限（秒） |
| `RETRY_INTERVAL_429` | `5.0` | 429 基础重试间隔（秒）；优先尊重上游 `Retry-After` |
| `RETRY_BACKOFF_429` | `true` | 是否对连续 429 启用指数退避和抖动 |
| `RETRY_BACKOFF_MAX_429` | `60` | 429 指数退避上限（秒） |

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
| `KEY_STICKY` | `120` | 成功 key 的分组粘性空闲超时（秒）；窗口内跳过分组重选和恢复复测，失败候选不建立窗口，当前 key 熔断时仍立即切换；`0` = 禁用 |
| `KEY_POOL_WAIT_TIMEOUT` | `120` | 所有候选 key 均熔断时的最长等待时间（秒）；超时返回 503，`0` = 不限制 |
| `KEY_TTFT_STALE_AFTER` | `300` | 真实首 Token 样本的有效期，以及复测失败后再次尝试的等待时间（秒） |
| `KEY_TTFT_RETEST_INTERVAL` | `60` | 便宜分组复测成功一次后，下一次确认复测的最小间隔（秒） |
| `KEY_TTFT_CONFIRMATIONS` | `2` | 升级或降回分组前要求的连续慢/快样本数 |
| `KEY_TTFT_HYSTERESIS` | `0.1` | 切换滞回比例；目标 5 秒、值为 0.1 时，超过 5.5 秒升级，低于 4.5 秒降回 |

## 在线同步

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KEY_POOL_SYNC_DEFAULT_ADAPTER` | `sub2api` | 管理页新增连接时默认使用的同步适配器 |
| `KEY_POOL_SYNC_URL` | `UPSTREAM_URL` | 管理页新增连接时预填的上游地址 |
| `KEY_POOL_SYNC_INTERVAL` | `300` | 自动同步周期（秒）；`0` = 仅手动同步 |
| `KEY_POOL_CREATE_DELAY` | `1.5` | 批量创建 key 时相邻请求的间隔（秒） |
| `KEY_POOL_SYNC_STATE_FILE` | `LOG_DIR/.key_pool_sync.json` | 同步连接、刷新令牌与最近成功号池的持久化文件 |

## 上游兼容

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IMAGE_UPSTREAM_USER_AGENT` | 空 | 图片请求转发时覆盖上游 User-Agent |
| `IMAGE_UPSTREAM_ORIGINATOR` | 空 | 图片请求转发时覆盖上游 Originator |

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

号池、在线同步调度和熔断状态是进程内状态，生产部署必须保持单 Uvicorn worker、单容器副本。当前不支持通过多 worker 或多副本横向扩容；多个进程会各自持有不同的号池，并竞争写入同步状态文件。
