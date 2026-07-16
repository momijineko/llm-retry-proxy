# llm-retry-proxy

一个面向 LLM API 的本地反向代理转发工具。上游服务（如 Coding Plan）过载返回 503 时，自动按固定间隔重试，直到拿到数据。完整透传请求/响应（含 SSE 流式），对客户端透明。

核心能力包括：固定间隔/指数退避重试、429 专用退避（优先 `Retry-After`）、竞速模式（请求竞速/滚动竞速）、多上游路由分流、号池多 key 降级、按天 JSONL 明细日志与累计汇总、内置可视化分析面板。

> **号池（Key Pool）** 用于中转站多 key 故障转移：按配置顺序从上到下依次使用，遇到 429/5xx 自动冷却切到下一个可用 key。与重试引擎松耦合，不配置时完全不介入请求流程。

**本项目仅推荐使用串行轮询请求，请慎用竞速模式，不当使用会为模型供应商的服务端点带来极大压力，严重可能会导致您被封号或造成其他经济损失！竞速模式未经过人工测试，开发者不对该项功能的完整性做任何保证！**

## 特性

- 通用反向代理：透传所有路径、Header、Body、Query
- 支持 SSE 流式响应（`text/event-stream`），重试只发生在首字节之前，开始流式后不再重试
- 503/502/504/529 自动重试，固定间隔
- 默认有最大重试次数保护，可一键关闭（无限重试直到成功）
- 响应头附带 `X-Forward-Attempts`，告知客户端本次请求重试了几次

## 快速开始

### 一键脚本

三端各有一个脚本，功能一致：自动创建虚拟环境 `.venv`、安装依赖、首次交互式生成 `.env` 并启动服务。首次运行会引导配置：

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

> PowerShell 若被执行策略拦截，加 `-ExecutionPolicy Bypass` 参数运行即可，仅对本次生效，不改动系统策略。

回车即采用方括号内默认值。后续再次运行会跳过配置直接启动。

### 手动

```bash
pip install -r requirements.txt

# 配置上游（默认指向讯飞星火 MaaS）
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

## 配置项

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
| `RETRY_STATUS_CODES` | `503,502,504,529,429` | 触发重试的上游状态码，逗号分隔 |
| `RETRY_BROAD` | `off` | 宽松重试/换key模式。开启后 5xx+429+401/403+网络异常 全部触发重试+换key，无需维护 `RETRY_STATUS_CODES` 白名单。适合号池场景：任何服务端/鉴权错误自动降级到其它 key。400/404/422 等请求级错误仍直接透传 |
| `HEDGE_MODE` | `off` | 竞速模式。`off`=串行重试（默认）；`race`=请求竞速，每轮一次性发 `MAX_CONCURRENT` 个，第一个 200 胜出取消其余；`stagger`=滚动竞速，按间隔交错发，非429错误立即补发（或按退避延迟） |
| `MAX_CONCURRENT` | `10` | 竞速模式下同时在飞/每轮并发的最大请求数 |
| `TIMEOUT` | `300` | 读写超时（秒），流式下指两次数据间最大间隔 |
| `CONNECT_TIMEOUT` | `10` | 连接上游超时（秒） |
| `PROVIDER` | `xfyun` | 供应商标签，写入每条重试记录，用于区分不同上游/账号 |
| `EXTRA_UPSTREAMS` | （空） | 额外上游路由，按路径前缀分流。格式 `prefix\|url\|provider`，多组逗号分隔。匹配前缀的请求去掉前缀后转发到对应 `url`，未匹配的走默认 `UPSTREAM_URL`。详见下方[多上游路由](#多上游路由) |
| `KEY_POOLS` | （空） | 号池配置（环境变量方式），启用后代理注入 key 并按倍率从低到高降级。格式 `key1;key2;key3`（用于默认上游）或 `url\|provider\|key1;key2`（多上游）。留空则保持透传客户端 key 的原有行为。详见下方[号池](#号池key-pool) |
| `KEY_POOL_FILE` | （空） | 号池 CSV 文件路径，**优先于 `KEY_POOLS`**。格式 `key,url,provider`，行序即优先级（上=便宜，下=贵）。详见下方[号池](#号池key-pool) |
| `KEY_COOLDOWN` | `30` | 单个 key 遇到 429/5xx 后的冷却时间（秒）。冷却期间优先跳过该 key，用更贵但可用的 key 降级 |
| `KEY_STICKY` | `60` | key 粘性持续时间（秒）。选定一个 key 后保持使用直到过期或被限流，避免频繁切换导致上游缓存失效。`0` = 禁用（每次都选最便宜可用 key） |
| `KEY_AUTH_HEADER` | `authorization` | 号池注入鉴权头的 header 名 |
| `KEY_AUTH_SCHEME` | `Bearer` | 鉴权 scheme 前缀（如 `Bearer`），设为空则只放裸 key |
| `LOG_DIR` | `logs` | 日志目录。明细按天拆分为 `retry_YYYY-MM-DD.jsonl`，累计汇总存 `_summary.json` |
| `LOG_RETENTION_DAYS` | `30` | 明细日志保留天数，超期自动删除（`0` = 不清理）。**累计汇总不受影响**，历史总量永久保留 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 多上游路由

通过 `EXTRA_UPSTREAMS` 可在同一个代理实例内同时转发多个上游端点，按**请求路径前缀**分流。匹配前缀的请求会**去掉前缀**后转发到对应上游；未匹配任何前缀的请求走默认 `UPSTREAM_URL`。

### 配置格式

```
EXTRA_UPSTREAMS=prefix|url|provider,prefix|url|provider,...
```

- `prefix`：请求路径前缀（如 `/anthropic`），不带前导斜杠也行
- `url`：对应上游地址（不要带尾斜杠）
- `provider`：写入日志的供应商标签，用于统计面板区分；留空则取 `prefix` 去掉斜杠

多组用逗号分隔，**长前缀优先匹配**（避免短前缀误吞）。

### 示例

默认上游是讯飞星火 MaaS（OpenAI 兼容），同时需要转发一个 Anthropic 端点：

```env
UPSTREAM_URL=https://maas-coding-api.cn-huabei-1.xf-yun.com/v2
PROVIDER=xfyun
EXTRA_UPSTREAMS=/anthropic|https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic|xfyun
```

| 客户端请求 | 匹配路由 | 实际转发到 |
|---|---|---|
| `POST /chat/completions` | 默认 | `https://.../v2/chat/completions` |
| `POST /anthropic/v1/messages` | `/anthropic` | `https://.../anthropic/v1/messages` |

> 前缀 `/anthropic` 被去掉，剩余路径 `v1/messages` 拼接到上游 `.../anthropic` 之后。鉴权头（如 `x-api-key`、`anthropic-version`）原样透传，对客户端完全透明。

### 多组路由

```env
EXTRA_UPSTREAMS=/anthropic|https://api.anthropic.com|anthropic,/gemini|https://generativelanguage.googleapis.com|gemini
```

统计面板的「按供应商」表格会按 `provider` 标签分别统计各上游的可用率/重试次数。「按路径」表格则按原始请求路径（含前缀）聚合。

## 号池（Key Pool）

通过 `KEY_POOLS` 配置一组 API key，代理会**自动注入鉴权头**并按**倍率从低到高**（cheap→expensive）逐 key 降级。适用于中转站不同分组对应不同倍率的场景：优先用便宜 key，遇到 429/5xx 自动切到更贵但可用的 key，被限的 key 冷却后恢复。

### 配置格式

支持两种配置方式，**CSV 文件优先于环境变量**。

#### 方式一：CSV 文件（推荐，适合批量配置）

```env
KEY_POOL_FILE=key_pool.csv
```

CSV 格式（首行表头，`#` 开头为注释）：

```csv
key,url,provider,label
sk-cheap-group,,,低倍率组
sk-normal-group,,,中倍率组
sk-premium-group,,,高倍率组
# 多上游示例（同一 url 的 key 自动归入同一池，按行序排列）
sk-other-key,https://other.com,other,备用站
```

- `key`（必填）：API key
- `url`（可选）：上游地址，留空 = `UPSTREAM_URL`
- `provider`（可选）：供应商标签，留空 = `PROVIDER`
- `label`（可选）：key 标签/备注，用于日志显示和统计面板分组。留空则用 key 前 8 位
- **行序即优先级**：上面的 = 低倍率（便宜），下面的 = 高倍率（贵）

> 项目附带 `key_pool.csv.example` 模板，复制为 `key_pool.csv` 即可使用。

#### 方式二：环境变量（适合少量 key）

```env
# 默认上游（UPSTREAM_URL）的 key 池，按倍率从低到高排列，分号分隔
KEY_POOLS=sk-cheap;sk-normal;sk-premium

# 多上游：每组 url|provider|key1;key2，逗号分隔
KEY_POOLS=https://aihub.top|aihub|sk-cheap;sk-premium,https://other.com|other|sk-other1;sk-other2
```

### 降级机制

1. 请求进来后，代理**剥离客户端原有的 Authorization 头**，注入号池中当前最便宜可用的 key
2. **串行模式（`HEDGE_MODE=off`）**：每次重试换下一个 key，立即降级（不等待退避）。只有**所有 key 都被冷却**时才进入现有退避等待
3. **竞速模式（`race`/`stagger`）**：每轮/每次发请求选当前最便宜可用 key，失败冷却该 key，下一轮自动选下一个。时序逻辑不变
4. 被 429/5xx 命中的 key 进入 `KEY_COOLDOWN`（默认 30 秒）冷却期，冷却期间优先跳过；429 带的 `Retry-After` 头会被优先采纳（取 `max(冷却时间, Retry-After)`）
5. **粘性保持**（`KEY_STICKY` 默认 60 秒）：选定一个 key 后保持使用，直到粘性过期或该 key 被限流。粘性期间即使更便宜的 key 恢复了也不切回，避免频繁切换导致上游缓存失效。设为 `0` 禁用（每次都选最便宜可用 key）
6. 全部 key 冷却时，`pick()` 返回最快到期的 key（**软冷却**，不阻塞请求），同时走现有指数退避等待

### 向后兼容

- `KEY_POOLS` 留空（默认）→ **完全保持原有行为**：透传客户端 Authorization 头，不注入任何 key
- 配置了 `KEY_POOLS` → 代理注入 key，客户端原有的 Authorization 头被覆盖

### 日志

每条重试记录新增 `key_id` 字段（key 前 8 字符），便于分析哪个 key 被限流。`/health` 端点也返回号池状态（各 key 的冷却/失败情况）。

### 自定义鉴权头

部分上游使用非标准鉴权头（如 `x-api-key`），可通过以下变量自定义：

```env
KEY_AUTH_HEADER=x-api-key
KEY_AUTH_SCHEME=           # 空值，直接放裸 key，不加 Bearer 前缀
```

## 重试行为说明

- 仅当上游返回 `RETRY_STATUS_CODES` 中的状态码时重试；其它状态码（含 200、400、401 等）原样透传。开启 `RETRY_BROAD=on` 后，触发条件改为规则：5xx + 429 + 401/403 + 网络异常均重试+换key，无需维护状态码白名单。
- **429 特殊处理**：429 使用 `RETRY_INTERVAL_429`（默认 5s）而非 `RETRY_INTERVAL`；若上游 429 响应携带 `Retry-After` 头（秒数或 HTTP 日期），则优先按该头等待，更精准地配合上游限流策略。
- **429 指数退避**（默认开启，`RETRY_BACKOFF_429=true`）：连续收到 429 时，等待时间按 `5→10→20→40→60s` 指数递增（基数 = `RETRY_INTERVAL_429`，倍率 = 2，上限 = `RETRY_BACKOFF_MAX_429`），并叠加 ±20% 随机抖动（jitter）以避免多 Agent 同步重试导致 429 雪崩。`Retry-After` 头仍优先，退避值取 `max(Retry-After, 指数退避值)`。设为 `false` 可回退到固定间隔模式。收到非 429 的重试状态码会重置 429 退避计数器。
- **非429 指数退避**（默认关闭，`RETRY_BACKOFF=false`）：开启后，连续收到 503/502/504/529 等非429重试状态码时，等待时间按 `1→2→4→8→16→32→60s` 指数递增（基数 = `RETRY_INTERVAL`，倍率 = 2，上限 = `RETRY_BACKOFF_MAX`），同样含 ±20% 抖动。适用于上游持续过载的场景，避免固定 1s 间隔不断冲击已过载的服务端。关闭时保持固定 `RETRY_INTERVAL` 行为。收到 429 会重置非429退避计数器。
- 对于流式请求：先读取上游响应头与状态码，若为 503 则丢弃 body 并重试；一旦上游开始返回 200 并流式输出，中途断流**不**重试（已向客户端发送部分数据，重试会导致内容错乱）。
- 请求异常（连接超时、网络断开等）同样会重试。
- 达到 `MAX_RETRIES` 后返回一个 503 JSON 错误给客户端；`MAX_RETRIES=0` 时永不放弃。

### 竞速模式（`HEDGE_MODE`）

串行模式（`off`）下，每次只发一个请求，拿到 503 后等待间隔再发下一个。竞速模式则**同时发多个请求，谁先 200 谁赢**：

**`HEDGE_MODE=race`（请求竞速）**：每轮一次性发 `MAX_CONCURRENT` 个请求，第一个返回 200 的胜出，其余立即取消。全部失败则等待间隔后下一轮。简单粗暴，最快命中，但对上游压力最大。

**`HEDGE_MODE=stagger`（滚动竞速）**：按 `RETRY_INTERVAL` 间隔交错发请求，不一次性全打满。任意一个返回 200 → 立即取消所有在飞请求。某个返回 503（非429）→ 立即补发一个新请求（`RETRY_BACKOFF=true` 时改为按指数退避延迟补发）。429 → 按 `RETRY_INTERVAL_429` 或 `Retry-After` 退避。`MAX_CONCURRENT` 限制同时在飞的上限。

| 特性 | `off` | `race` | `stagger` |
|---|---|---|---|
| 并发 | 串行，1 个 | 每轮 N 个齐发 | 交错，逐步增长 |
| 命中速度 | 最慢 | 最快 | 中等 |
| 上游压力 | 最低 | 最高 | 中等 |
| 适用场景 | 稳定上游 | 间歇 503，求快 | 间歇 503，求稳 |

> **可用率口径**：统计面板区分两个口径——**上游可用率**（首次尝试即成功的请求占比，`retries==0 && final_status<400`，反映上游本身健康度）与**下游可用率**（经重试后最终返回客户端成功的占比，`final_status<400`，反映代理后的最终效果）。两者差值即为重试挽救的请求数，直观体现重试代理的价值。

## 重试记录

每个请求处理完成（成功或放弃）后，向 `LOG_DIR`（默认 `logs/`）追加一行 JSON 明细，便于后续做数据分析。

**存储结构**：

```
logs/
  retry_2026-07-07.jsonl   # 按天拆分的明细（每请求一行 JSON）
  retry_2026-07-06.jsonl
  ...
  _summary.json            # 累计汇总（全局总量，原子写，不随明细清理而丢失）
```

- **明细按天拆分**：单文件不会无限增长，天然按时间分片。
- **累计汇总**：`_summary.json` 保存全局累计指标（总请求/总重试/各模型各供应商累计计数），每次请求增量更新。即使明细被自动清理，累计总量永久保留。
- **自动清理**：启动时删除超过 `LOG_RETENTION_DAYS` 天的明细文件（`0` = 不清理），`_summary.json` 不受影响。
- **客户端 IP**：实时日志与新增明细记录会显示客户端 IP；经反向代理时依次读取 `CF-Connecting-IP`、`X-Forwarded-For`、`X-Real-IP`，否则使用直连地址。
- **旧格式迁移**：首次启动若检测到旧的单文件 `retry_log.jsonl`，自动按日期拆分到 `logs/` 并重建累计汇总，旧文件重命名为 `.bak`。

字段说明：

| 字段 | 说明 |
|---|---|
| `ts` | 请求结束时间戳（ISO，毫秒精度） |
| `method` | HTTP 方法 |
| `path` | 请求路径 |
| `provider` | 供应商，取自 `PROVIDER` 环境变量 |
| `model` | 模型名，从请求 body 的 `model` 字段解析；无则为空字符串 |
| `upstream_status` | 最后一次上游响应的状态码（请求异常时为 0） |
| `final_status` | 返回给客户端的状态码（放弃重试时为 503） |
| `attempts` | 总尝试次数（含首次） |
| `retries` | 重试次数 = `attempts - 1` |
| `duration_s` | 总耗时（秒） |
| `succeeded` | 是否最终拿到 2xx/3xx 响应（`final_status < 400`，4xx/5xx 视为失败） |
| `retry_codes` | 重试过程中上游返回的错误码列表，如 `[503, 503, 429]`。无重试时为空数组 `[]`。用于统计面板的错误码分析 |
| `key_id` | 号池模式下使用的 key 标签（CSV 中 `label` 列，未设则用 key 前 8 字符），未启用号池时为空字符串。统计面板「按 key」表格按此字段分组 |

示例：

```json
{"ts":"2026-07-07T11:52:35.123","method":"POST","path":"/chat/completions","provider":"xfyun","model":"spark-v4","upstream_status":200,"final_status":200,"attempts":3,"retries":2,"duration_s":0.852,"succeeded":true,"retry_codes":[503,503]}
```

快速分析示例：

```bash
# 各模型重试次数统计（当天）
jq -s 'group_by(.model) | map({model:.[0].model, n:length, avg_retries:(map(.retries)|add/length)})' logs/retry_$(date +%Y-%m-%d).jsonl

# 用 pandas
python -c "import pandas as pd; df=pd.read_json('logs/retry_$(date +%Y-%m-%d).jsonl',lines=True); print(df.groupby('model')['retries'].agg(['count','mean','max']))"
```

## 健康检查

```bash
curl http://127.0.0.1:8080/health
# {"status":"ok","upstream":"https://maas-coding-api.cn-huabei-1.xf-yun.com/v2","routes":[{"prefix":"/","upstream":"https://.../v2","provider":"xfyun"}],"key_pools":{}}
```

`routes` 列出所有已配置路由（`/` 为默认上游）。`key_pools` 列出号池状态（各 key 的冷却/失败情况），未配置号池时为空对象 `{}`。

## 可视化分析面板

服务内置一个重试数据分析页面，基于日志实时统计，浏览器直接访问：

```
http://127.0.0.1:8080/stats
```

支持按时间范围切换：**今天 / 7天 / 30天 / 全部**。面板分两大区域：

- **累计总览**：从 `_summary.json` 读取的全量历史指标（含已归档/清理的明细），O(1) 不扫文件
- **明细分析**：对所选时间范围内的明细文件做完整聚合

面板内容：

- **总览**：总请求数、总重试次数、上游可用率、下游可用率、失败请求数、P95 耗时
- **可用性分析**：当前连续（成功/失败）、最长连续失败、失败总数；各模型可用率柱状图（上游/下游对比，灰柱为上游、彩柱为下游）；失败原因（上游错误码，含触发重试的请求）；可用率时间趋势（上游/下游双线，含 95% 基准线）
- **状态码分布**：上游状态码分布饼图（按实际状态码，如 200/401/503）；上游状态码柱状图（含重试过程中的错误码）；各模型状态码构成（堆叠柱状图，按实际状态码）
- **重试分析**：重试次数分布直方图；重试负担分桶（0/1-5/6-20/21-50/>50 次）；上游错误码分布（含重试过程中的所有错误码）
- **耗时分析**：平均/P50/P95/P99/最大耗时卡片；最慢/最快请求 Top 8 并排明细表（最快仅统计成功且耗时>0 的请求）
- **时间模式**：按时段（0-23 点）请求数+上游/下游可用率；时间趋势（请求/重试/失败数）
- **明细表**：按供应商、按模型（含上游/下游可用率、P95、主要失败码）、按路径 Top 10

支持手动刷新与 15 秒自动刷新。页面通过 CDN 加载 ECharts，需能访问外网。

数据接口（可自行集成）：`GET /stats/api`，返回聚合后的 JSON。

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
