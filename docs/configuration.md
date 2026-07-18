# 配置项

[返回 README](../README.md)

所有配置通过 `.env` 或环境变量设置。可直接复制项目中的 `.env.example` 作为起点。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `UPSTREAM_URL` | `https://maas-coding-api.cn-huabei-1.xf-yun.com/v2` | 上游地址，不要带尾斜杠 |
| `LISTEN_HOST` | `0.0.0.0` | 监听地址 |
| `LISTEN_PORT` | `8080` | 监听端口 |
| `RETRY_INTERVAL` | `1.0` | 重试间隔（秒），适用于 503/502/504/529 等，作为非429指数退避的基数 |
| `RETRY_BACKOFF` | `false` | 非429状态码（503/502/504/529）指数退避开关。**默认关闭**，保持固定间隔。开启后连续失败时等待时间按 `1→2→4→8→16→32→60s` 指数递增（含 ±20% 抖动），缓解上游持续过载 |
| `RETRY_BACKOFF_MAX` | `60` | 非429指数退避等待上限（秒）。退避值不会超过此值 |
| `RETRY_INTERVAL_429` | `5.0` | **429 专用**重试间隔（秒），作为指数退避的基数。若上游 429 响应带 `Retry-After` 头，则优先使用该头指定的等待时间 |
| `RETRY_BACKOFF_429` | `true` | 429 指数退避开关。**默认开启**。开启后连续 429 时等待时间按 `5→10→20→40→60s` 指数递增（含 ±20% 抖动），避免多 Agent 同步重试触发 429 雪崩。关闭则回退到固定 `RETRY_INTERVAL_429` |
| `RETRY_BACKOFF_MAX_429` | `60` | 429 指数退避等待上限（秒）。退避值不会超过此值 |
| `MAX_RETRIES` | `60` | 最大重试次数。**设为 `0` 表示无限重试**（关闭限制） |
| `RETRY_STATUS_CODES` | `503,502,504,524,529,429` | 触发重试的上游状态码，逗号分隔 |
| `RETRY_BROAD` | `off` | 宽松重试/换key模式。开启后 5xx+429+401/403+网络异常 全部触发重试+换key，无需维护 `RETRY_STATUS_CODES` 白名单。适合号池场景；400/404/422 等请求级错误仍直接透传 |
| `HEDGE_MODE` | `off` | 竞速模式。`off`=串行重试（默认）；`race`=请求竞速；`stagger`=滚动竞速 |
| `MAX_CONCURRENT` | `10` | 竞速模式下同时在飞/每轮并发的最大请求数 |
| `TIMEOUT` | `300` | 读写超时（秒），流式下指两次数据间最大间隔 |
| `CONNECT_TIMEOUT` | `10` | 连接上游超时（秒） |
| `TRUST_ENV` | `false` | 是否让 HTTP 客户端读取系统代理等环境变量 |
| `PROVIDER` | `xfyun` | 供应商标签，写入每条重试记录，用于区分不同上游/账号 |
| `EXTRA_UPSTREAMS` | （空） | 额外上游路由，按路径前缀分流。格式 `prefix\|url\|provider`，多组逗号分隔。详见[多上游路由](routing.md) |
| `KEY_POOLS` | （空） | 号池配置（环境变量方式）。详见[号池与在线同步](key-pool.md) |
| `KEY_POOL_FILE` | （空） | 号池 CSV 文件路径，**优先于 `KEY_POOLS`**。详见[号池与在线同步](key-pool.md) |
| `KEY_COOLDOWN` | `30` | 旧版兼容值；未配置 `KEY_COOLDOWN_5XX` 时作为 5xx/其他错误的基础冷却时间 |
| `KEY_COOLDOWN_5XX` | `30` | key 遇到 5xx 或非主机级传输异常后的基础熔断时间（秒） |
| `KEY_COOLDOWN_429` | `60` | key 遇到 429 后的基础熔断时间（秒）；上游 `Retry-After` 更长时优先采用 |
| `KEY_COOLDOWN_AUTH` | `1800` | key 遇到 401/403 后的基础熔断时间（秒） |
| `KEY_COOLDOWN_MAX` | `3600` | 连续同类错误指数熔断的上限（秒），不截短上游 `Retry-After` |
| `KEY_COOLDOWN_BACKOFF` | `true` | 同类错误在熔断到期后再次失败时，将熔断时间按 `1→2→4...` 倍延长；成功后清零 |
| `KEY_STICKY` | `120` | key 粘性空闲超时（秒）。`0` = 禁用 |
| `KEY_POOL_WAIT_TIMEOUT` | `120` | 全部 key 熔断时等待可用 key 的最长时间（秒） |
| `KEY_AUTH_HEADER` | `authorization` | 号池注入鉴权头的 header 名 |
| `KEY_AUTH_SCHEME` | `Bearer` | 鉴权 scheme 前缀；设为空则只放裸 key |
| `LOG_DIR` | `logs` | 日志目录。明细按天拆分为 `retry_YYYY-MM-DD.jsonl`，累计汇总存 `_summary.json` |
| `LOG_FILE` | `retry_log.jsonl` | 旧版单文件日志路径，仅用于自动迁移 |
| `ADMIN_PASSWORD` | 空 | 管理页面密码；未配置时 `/stats*`、`/logs*` 禁用。兼容旧 `ADMIN_TOKEN` |
| `ADMIN_COOKIE_SECURE` | `false` | HTTPS 部署时设为 `true`，限制登录 Cookie 仅通过 HTTPS 发送 |
| `PROXY_API_KEY` | 空 | 下游使用号池的凭据；配置后，无正确凭据的请求仅作纯代理转发 |
| `LOG_RETENTION_DAYS` | `30` | 明细日志保留天数，超期自动删除（`0` = 不清理）。累计汇总不受影响 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `KEY_POOL_SYNC_DEFAULT_ADAPTER` | `sub2api` | 管理页新连接默认使用的同步适配器 |
| `KEY_POOL_SYNC_URL` | `UPSTREAM_URL` | 管理页新连接默认上游地址 |
| `KEY_POOL_SYNC_INTERVAL` | `300` | 自动同步周期（秒）；`0` 表示仅手动同步 |
| `KEY_POOL_CREATE_DELAY` | `1.5` | 批量创建 key 时相邻请求的间隔（秒） |
| `KEY_POOL_SYNC_STATE_FILE` | `LOG_DIR/.key_pool_sync.json` | 同步连接、刷新令牌和最近成功号池的持久化文件 |
| `IMAGE_UPSTREAM_USER_AGENT` | 空 | 图片请求转发时覆盖上游 User-Agent |
| `IMAGE_UPSTREAM_ORIGINATOR` | 空 | 图片请求转发时覆盖上游 Originator |
| `DLP_MODE` | `off` | 敏感信息处理：`off`、`audit`、`redact` 或 `block`。详见[日志、DLP 与记录格式](logging-and-dlp.md) |
| `DLP_RULES` | 见 `.env.example` | 启用的 DLP 规则 |
| `DLP_RULE_FILE` | `retry_proxy/dlp_rules.yaml` | 自定义 DLP 规则文件 |
| `DLP_EXEMPT_START` | `[[ALLOW_SENSITIVE]]` | 主动豁免区间的起始标记 |
| `DLP_EXEMPT_END` | `[[/ALLOW_SENSITIVE]]` | 主动豁免区间的结束标记 |
| `DLP_STRIP_EXEMPT_MARKERS` | `true` | 转发前是否移除主动豁免标记 |
| `DLP_MAX_BODY_BYTES` | `16777216` | DLP 扫描的请求体大小上限（字节） |

## Docker 部署补充

`compose.yaml` 会将 `LOG_DIR` 映射到宿主机的 `./logs`，并将 `KEY_POOL_FILE` 默认指向容器内的 `/app/key_pool.csv`。部署前请准备 `.env`；需要号池时复制 `key_pool.csv.example` 为 `key_pool.csv`。
