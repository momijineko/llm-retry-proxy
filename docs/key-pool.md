# 号池（Key Pool）

[返回 README](../README.md)

通过 `KEY_POOLS` 配置一组 API key，代理会**自动注入鉴权头**并按**倍率从低到高**（cheap→expensive）逐 key 降级。适用于中转站不同分组对应不同倍率的场景：优先用便宜 key，遇到 429/5xx 自动切到更贵但可用的 key，被限的 key 冷却后恢复。

## 配置格式

支持两种配置方式，**CSV 文件优先于环境变量**。

### CSV 文件（推荐，适合批量配置）

```env
KEY_POOL_FILE=key_pool.csv
```

CSV 格式（首行表头，`#` 开头为注释）：

```csv
key,url,provider,label,sort,models,paths,auth_header,auth_scheme
sk-cheap-group,,,低倍率组,0.02,,,,
sk-normal-group,,,中倍率组,0.1,,,,
sk-premium-group,,,高倍率组,0.2,,,,

# 多上游示例（同一 url 的 key 自动归入同一池，按 sort 排列）
sk-other-key,https://other.com,other,备用站,0.1,,,,
```

- `key`（必填）：API key
- `url`（可选）：上游地址，留空 = `UPSTREAM_URL`
- `provider`（可选）：供应商标签，留空 = `PROVIDER`
- `label`（可选）：key 标签/备注，用于日志显示和统计面板分组。留空则用 key 前 8 位
- `sort`（可选）：数值顺序或倍率。每个号池按数值从小到大选择 key；相同值保持 CSV 行序。日志和统计标识显示为 `label|sort`
- `models`（可选）：该行 key 匹配的模型 glob，多个用分号分隔，例如 `gpt-image-*;imagen-*`
- `paths`（可选）：该行 key 匹配的路径 glob，多个用分号分隔，例如 `images/*;v1/images/*`
- `auth_header`（可选）：该上游注入 Key 使用的 Header，例如 `authorization` 或 `x-api-key`；留空使用全局 `KEY_AUTH_HEADER`
- `auth_scheme`（可选）：该上游鉴权 scheme，例如 `Bearer`；留空使用全局 `KEY_AUTH_SCHEME`，填写 `-` 或 `none` 表示裸 Key
- 未配置 `sort` 时保持原行序并排在已配置有效 `sort` 的 key 之后，兼容旧 CSV
- 号池以规范化后的 `url` 为唯一标识；同一 URL 的所有行必须使用相同 `provider`。分组、倍率或用途差异请放在 `label`、`sort`、`models`、`paths` 中

CSV 在进程启动时读取，运行期间修改文件不会自动热加载，需要重启服务。网站同步号池支持运行时热更新，规则见下文。

当任一专用行命中模型或路径时，请求只在这些命中行之间选 key；未命中时只使用 `models` 和 `paths` 都为空的普通行。专用池与普通池分别维护粘性状态，专用 key 冷却时不会泄漏回普通分组。

```csv
key,url,provider,label,sort,models,paths,auth_header,auth_scheme
sk-normal,https://aihub.top,aihub,普通组,0.1,,,,
sk-image,https://aihub.top,aihub,生图组,1,gpt-image-*;imagen-*,images/*;v1/images/*,,
```

> 项目附带 `key_pool.csv.example` 模板，复制为 `key_pool.csv` 即可使用。

### 环境变量（适合少量 key）

```env
# 默认上游（UPSTREAM_URL）的 key 池，按倍率从低到高排列，分号分隔
KEY_POOLS=sk-cheap;sk-normal;sk-premium

# 多上游：每组 url|provider|key1;key2，逗号分隔
KEY_POOLS=https://aihub.top|aihub|sk-cheap;sk-premium,https://other.com|other|sk-other1;sk-other2
```

## 降级机制

1. 配置 `PROXY_API_KEY` 后，下游把它作为 Bearer API key 发送即可使用号池；代理随后**剥离该头**，注入号池中当前最便宜可用的上游 key
2. **串行模式（号池固定使用）**：每次重试换下一个 key，立即降级（不等待退避）。只有**所有 key 都被冷却**时才进入现有退避等待
3. 普通请求的 `HEDGE_MODE` 不会改变号池请求的串行调度；号池仍按每个 key 的独立退避和熔断状态切换
4. 分状态熔断：5xx 默认 30 秒、429 默认 60 秒、401/403 默认 30 分钟；主机连接失败不归咎于单个 key，不触发 key 熔断
5. 同一 key 在熔断到期后再次遇到同类错误，冷却时间按 `1→2→4...` 倍延长，默认最高 1 小时；任意成功响应会立即清空该 key 的连续失败级别
6. 429 带 `Retry-After` 时取 `max(指数熔断时间, Retry-After)`，即使超过 `KEY_COOLDOWN_MAX` 也尊重上游时间
7. **粘性保持**（`KEY_STICKY` 默认 120 秒）：选定一个 key 后，后续请求会不断续期；只有持续空闲超过 2 分钟或当前 key 被限流，下一次才从号池开头重新选择。设为 `0` 禁用
8. 全部 key 熔断时，请求等待最早到期的 key 再进行探测，不会穿透熔断继续高频请求；等待超过 `KEY_POOL_WAIT_TIMEOUT`（默认 120 秒）后返回 503，客户端断开后也会立即取消

## 向后兼容

- `KEY_POOLS` 留空（默认）→ **完全保持原有行为**：透传客户端 Authorization 头，不注入任何 key
- 配置了 `KEY_POOLS`、未配置 `PROXY_API_KEY` → 保持旧行为，代理直接注入号池 key
- 同时配置 `PROXY_API_KEY` → 只有携带正确 Bearer key 的请求使用号池；未携带或不匹配的请求保留自身鉴权头，按纯代理模式转发
- 携带正确 `PROXY_API_KEY` 但目标上游的号池不存在时返回 503，不会把代理号池凭据透传给上游

## 日志中的号池信息

每条重试记录包含实际号池 `key_pool`、最终使用的 `key_id` 和逐次尝试的 `key_attempts`，统计面板据此按号池分别计算各 key 的可用率，并用对比图和精简表格展示。只有当前配置了号池时才显示该版块；多号池会分别展示，并由页面右上角的供应商筛选统一控制。重复 key 会被去重；重复标签会自动追加不可逆短指纹，避免统计串线。

## 自定义鉴权头

部分上游使用非标准鉴权头（如 `x-api-key`），可通过以下变量自定义：

```env
KEY_AUTH_HEADER=x-api-key
KEY_AUTH_SCHEME=           # 空值，直接放裸 key，不加 Bearer 前缀
```

## 网站同步号池

配置 `ADMIN_PASSWORD` 后访问 `/admin/key-pools`，可添加一个或多个上游连接。每个连接使用统一配置：

| 字段 | 说明 |
|---|---|
| `adapter` | 上游类型；当前内置 `sub2api` |
| `base_url` | 上游站点地址，例如 `https://aihub.top` |
| `route_prefix` | 下游访问该上游使用的代理前缀，例如 `/aihub`；转发时会去掉此前缀 |
| `provider` | 本地日志与统计使用的供应商标签 |
| `credentials` | 由适配器声明的认证字段；不会通过状态接口返回 |

`sub2api` 适配器使用邮箱密码完成首次登录，随后仅保存刷新令牌，密码不会落盘。它同步完整 Key、Key 名称、分组名称、启用状态、默认倍率及用户专属倍率。已有 Key 的 `models`、`paths` 规则和运行时熔断状态会在热更新时保留。

同步成功后代理路由和新号池立即生效，无需重启。同一 URL 同时存在 CSV 静态池和网站同步池时，已同步的网站号池拥有运行时优先级。若连接复用 `EXTRA_UPSTREAMS` 中同前缀、同 `provider` 的路由，在线来源会同时覆盖该前缀的环境目标和号池；因此同步站点 `base_url` 与测试环境 URL 不同时，请求会成对切换到在线正式地址和正式 Key。断开连接只清除登录会话并继续使用最后一次成功快照；删除同步来源后才会恢复环境路由和进程启动时加载的静态池。若没有静态池，该 URL 的本地号池会被移除。

管理页路由会与 `EXTRA_UPSTREAMS` 合并；前缀和 `provider` 都相同时，在线同步来源在运行期间覆盖环境变量中的测试目标，删除来源后恢复环境目标。供应商不同则拒绝绑定，防止 Key 注入错误上游。连接状态、代理前缀与最后一次成功配置写入 `KEY_POOL_SYNC_STATE_FILE`，默认位于 `LOG_DIR/.key_pool_sync.json` 且权限为 `0600`；上游暂时不可用时继续使用最后一次成功配置。删除号池会撤销登录会话，并删除同步配置和由管理页创建的代理路由，但不会删除上游平台中的 Key。统计、日志、号池页面共用登录后的顶部导航。

> 在线同步状态和熔断状态保存在进程内存中，当前仅支持单 Uvicorn worker、单容器副本运行。不要使用 `uvicorn --workers`、Gunicorn 多 worker 或横向扩容多个副本，否则各进程的号池和刷新令牌可能分裂。

分组目录支持多选创建 Key、一键补齐缺失分组，以及选择分组清空远程 Key（操作前会二次确认）。新建 Key 默认直接使用分组名；适配器仍可通过显式 `name_prefix` 选项覆盖。Key 列表支持按分组或倍率排序，排序只影响展示。

每个在线同步来源可以在号池页面选择运行时分组调度策略：

- **最低倍率优先**：保持原有行为，按同步得到的倍率从低到高选择。
- **首 Token 优先**：根据成功响应的首个有效响应体或 SSE Token 计算分组级 EWMA，优先选择实测最快的分组。
- **兼顾两者**：在实测首 Token 不超过“可接受首 Token 上限”的分组中选择倍率最低者；若全部超过上限，则选择实测最快者。冷启动按倍率从低到高逐级探测，任一分组达到上限要求后便停止探测更高倍率；运行期间只低频复测当前选择以下的更低倍率分组。

首 Token 指标在同一在线分组的 Key 间共享，保存在运行时内存中；同步热更新会保留未删除 Key 的样本，进程重启后重新学习。SSE 在首个有效 Token 到达时记录；Responses WebSocket 则从每个 `response.create` 发出时开始计时，在首个有效上游增量到达时记录，并忽略 `generate=false` 预热及响应元数据事件。“首 Token 优先”会按倍率探测所有无样本分组，并在运行期间低频复测其它分组；“兼顾两者”则限制为达到目标所需的逐级探测和更低倍率复测，避免用真实业务请求主动探测不必要的高倍率分组。静态 CSV 号池继续使用最低倍率优先，不受在线来源策略影响。

号池页面的“检测可用性”只在手动点击时执行，不会后台轮询模型。检测最多同时发起 2 个请求，按分组依次尝试 Key；组内任一 Key 成功后立即停止检测该组。检测期间页面会读取本地管理状态，已成功分组的首 Token 无需等待整批结束即可显示；状态刷新不会向上游增加请求。模型检测使用页面指定的检测模型调用上游 `POST /v1/chat/completions`，只包含固定的最小提示并设置 `max_tokens=1`。真实生成返回 `2xx/3xx` 才视为可用，同时将不含并发排队时间的完成耗时写入该分组的首 Token 估算。模型不存在、请求格式不兼容等请求级 `4xx` 会报告为“不支持该模型或请求”，但不会污染用于其它模型的全局分组健康状态；`401/403`、`429`、`5xx` 或网络异常导致组内全部 Key 失败时才熔断整个分组。检测熔断沿用 `KEY_COOLDOWN_5XX`，到期自动恢复，也可以从页面解除单个分组或全部分组的熔断。

管理页的分组目录还支持配置分组路由映射。为某个分组填写模型通配符和路径通配符后，该分组同步到的所有 Key 会继承这些规则。例如 `Image2-超分4k` 可配置 `paths=v1/images/*`、`models=image2-*`；请求匹配这些规则时才会从该分组取 Key。规则按分组 ID 保存在同步状态中，重启后仍然有效。

### 自动端点与模型能力隔离

在线同步适配器可以为分组返回结构化的路由能力。`sub2api` 会同步分组的 `platform`、支持的模型范围、启用的模型列表、生图权限和 OpenAI Messages 调度权限，并据此生成自动能力约束。代理先根据去掉代理前缀后的请求路径识别 `chat`、`responses`、`messages`、`images`、`embeddings`、`audio` 或 Gemini 原生端点，再解析正文或 Gemini 路径中的模型名。

自动能力是硬约束：代理先排除端点协议、模型范围或生图能力不兼容的分组，再应用管理页中的手工 `models`、`paths` 通配符。手工规则只能进一步缩小候选范围，不能将已被自动能力排除的分组重新加入。所有重试、熔断、粘性和倍率/首 Token 调度都只在最终候选分组内进行。

当在线号池包含能力元数据、请求端点可识别，但没有兼容 Key 时，代理返回 HTTP 403 和 `key_pool_no_compatible_route`，不会向上游发送请求。错误体同时包含请求模型、端点族、供应商、上游和未匹配原因。静态 CSV、旧同步快照以及未返回可靠能力元数据的适配器继续沿用原有 `models`、`paths` 与默认池行为。

当前 `sub2api` 平台映射如下：`openai` 使用 Chat、Responses、Embeddings 和 Audio；开启 Messages 调度后也可使用 Messages；`anthropic` 使用 Messages；`gemini` 使用 Gemini 原生端点；`antigravity` 使用 Chat、Messages 和 Gemini 原生端点；`grok` 使用 Chat 和 Responses。开启生图权限的分组额外获得 Images 能力。未知平台不根据名称猜测，保持旧选择行为。

通用调度配置：

```env
KEY_POOL_SYNC_DEFAULT_ADAPTER=sub2api
KEY_POOL_SYNC_URL=https://aihub.top
KEY_POOL_SYNC_INTERVAL=300
# 创建/补齐 Key 时相邻请求的间隔（秒）
KEY_POOL_CREATE_DELAY=1.5
# 可选：图片请求转发到上游时覆盖客户端身份
IMAGE_UPSTREAM_USER_AGENT=
IMAGE_UPSTREAM_ORIGINATOR=
# KEY_POOL_SYNC_STATE_FILE=/app/logs/.key_pool_sync.json
```

新增其它中转适配时，实现 `PoolSyncAdapter` 的认证与标准化接口，并按需覆盖 `routing_capabilities(group)` 返回可靠能力；无法可靠判断时返回空对象即可保留旧行为。适配器在 `retry_proxy/sync_adapters/__init__.py` 注册后，管理 API、持久化、定时任务和热替换逻辑不需要修改。

不同中转站还可以在标准化 entry 中返回 `auth: {"header": "x-api-key", "scheme": ""}`。鉴权配置随 Key 进入候选池和重试链路，不再要求所有上游共用同一套全局 Header；未提供该字段的适配器继续使用全局配置。若站点不是 OpenAI Chat 探测格式，适配器应覆盖 `availability_request(source, model)`，返回自己的检测 URL、请求体和附加 Header。
