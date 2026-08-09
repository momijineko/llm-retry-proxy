"""配置项元数据表（配置中心的唯一事实源）

以 .env.example 全集 84 项为准，每项描述键的类型、默认值、枚举、
分组、敏感标记与生效类别。配置页 GET/POST /admin/settings、校验逻辑
与 tests/test_settings_meta.py 的一致性断言均以此表为唯一来源
"""

from dataclasses import dataclass


# 生效类别：hot=保存后立即生效，restart=保存后需重启进程，
# rebuild=仅构建期使用（写入 .env 后重建镜像生效）
HOT = "hot"
RESTART = "restart"
REBUILD = "rebuild"

# 分组顺序与页面侧栏导航分组对应（运行/请求/号池/防护），导航顺序变更时需同步调整
GROUPS = (
    "Docker 与运行环境",
    "服务与访问控制",
    "上游、路由与网络",
    "日志",
    "Codex Responses WebSocket 桥接 (SSE2WS)",
    "连接与响应超时",
    "重试与退避",
    "竞速模式",
    "号池来源与鉴权",
    "号池熔断与选择",
    "在线同步",
    "Token 统计",
    "上游兼容",
    "请求正文敏感信息防护",
)


@dataclass(frozen=True)
class ConfigItem:
    """单个配置项的定义"""

    key: str
    type: str = "str"
    default: str = ""
    enum: tuple = ()
    group: str = "其他"
    secret: bool = False
    apply: str = RESTART
    description: str = ""
    # Settings 属性名；留空时取 key.lower()（如 KEY_POOLS -> key_pools_raw 等特例需显式指定）
    attr: str = ""
    # 配置页面展示的中文名（如 TZ -> 容器时区）
    name: str = ""
    # 是否不作为独立配置项渲染（构建期配置，仅展示为分组描述信息）
    hidden: bool = False
    # 数值输入框后展示的单位（如 秒/天）；空表示无单位
    unit: str = ""


CONFIG_ITEMS: list[ConfigItem] = [
    # ---------------------------------------------------------------- Docker 与运行环境
    ConfigItem("DOCKER_REGISTRY", "str", "docker.io", group="Docker 与运行环境",
               apply=REBUILD, description="Docker 仓库域名，换镜像站时只改这里", name="Docker 仓库域名"),
    ConfigItem("PYTHON_BASE_IMAGE", "str", "library/python:3.12-slim", group="Docker 与运行环境",
               apply=REBUILD, description="基础镜像命名空间/镜像名:tag", name="基础镜像"),
    ConfigItem("PIP_INDEX_URL", "str", "https://pypi.tuna.tsinghua.edu.cn/simple",
               group="Docker 与运行环境", apply=REBUILD, description="Docker 构建使用的 PyPI 镜像",
               name="PyPI 镜像源"),
    ConfigItem("TZ", "str", "Asia/Shanghai", group="Docker 与运行环境",
               apply=RESTART, description="容器时区（影响日志时间）", name="容器时区"),

    # ---------------------------------------------------------------- 服务与访问控制
    ConfigItem("LISTEN_HOST", "str", "0.0.0.0", group="服务与访问控制",
               apply=RESTART, description="监听地址（compose 环境已覆盖，改 compose 才生效）",
               name="监听地址"),
    ConfigItem("LISTEN_PORT", "int", "8080", group="服务与访问控制",
               apply=RESTART, description="监听端口（compose 端口映射需同步修改）", name="监听端口"),
    ConfigItem("UVICORN_LOOP", "enum", "auto", ("auto", "asyncio"), group="服务与访问控制",
               apply=RESTART, description="Uvicorn 事件循环；旧内核兼容模板会覆盖为 asyncio",
               name="事件循环"),
    ConfigItem("ADMIN_PASSWORD", "str", "", group="服务与访问控制", secret=True,
               apply=RESTART, description="管理页面密码；未配置时 /stats*、/logs* 和 /key-pools 禁用",
               name="管理密码"),
    ConfigItem("SETTINGS_PAGE_ENABLED", "bool", "false", group="服务与访问控制",
               apply=RESTART, description="是否启用配置中心页面（/settings）；关闭时页面与接口返回 404",
               name="配置页面开关"),
    ConfigItem("ADMIN_COOKIE_SECURE", "bool", "false", group="服务与访问控制",
               apply=RESTART, description="HTTPS 部署时设为 true，使登录 Cookie 仅通过 HTTPS 发送",
               name="Cookie 安全标记"),
    ConfigItem("ADMIN_TOKEN", "str", "", group="服务与访问控制", secret=True,
               apply=RESTART, description="兼容旧配置：ADMIN_PASSWORD 留空时回退读取",
               name="兼容旧管理令牌"),
    ConfigItem("PROXY_API_KEY", "str", "", group="服务与访问控制", secret=True,
               apply=HOT, description="下游使用号池的凭据；不匹配时仅作普通透传",
               name="下游号池凭据"),
    ConfigItem("IP_BLACKLIST", "csv", "", group="服务与访问控制",
               apply=RESTART, description="拒绝访问的客户端 IP 或 CIDR，逗号分隔；同时覆盖 HTTP 与 WebSocket",
               name="IP 黑名单"),
    ConfigItem("IP_AUTO_BAN_THRESHOLD", "int", "20", group="服务与访问控制",
               apply=RESTART, description="窗口内访问不同路径达到该数量时动态封禁；0=关闭",
               name="动态封禁阈值"),
    ConfigItem("IP_AUTO_BAN_WINDOW", "float", "10", group="服务与访问控制",
               apply=RESTART, unit="秒", description="动态封禁检测窗口（秒）", name="封禁检测窗口"),
    ConfigItem("IP_AUTO_BAN_DURATION", "float", "0", group="服务与访问控制",
               apply=RESTART, unit="秒", description="动态封禁持续时间（秒）；0=永久封禁",
               name="封禁持续时间"),
    ConfigItem("IP_AUTO_BAN_EXEMPT", "csv", "127.0.0.0/8,::1", group="服务与访问控制",
               apply=RESTART, description="不参与动态封禁的 IP/CIDR；静态黑名单仍优先",
               name="动态封禁豁免"),
    ConfigItem("TRUSTED_PROXY_IPS", "csv", "127.0.0.0/8,::1,172.16.0.0/12", group="服务与访问控制",
               apply=RESTART, description="可信反向代理的直连 IP 或 CIDR；仅来自这些地址的转发头会被采信",
               name="可信代理 IP"),
    ConfigItem("IP_BAN_STATE_FILE", "str", "logs/.ip_bans.json", group="服务与访问控制",
               apply=RESTART, description="动态封禁状态文件；留空时使用 LOG_DIR/.ip_bans.json",
               name="封禁状态文件"),
    ConfigItem("PROVIDER_ALIASES", "str", "", group="服务与访问控制",
               apply=HOT, description="统计 provider 显示别名：每行一条，把统计中的原名称显示为别名",
               name="统计 Provider 显示别名"),

    # ---------------------------------------------------------------- 上游、路由与网络
    ConfigItem("UPSTREAM_URL", "str", "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2",
               group="上游、路由与网络", apply=RESTART, description="默认上游地址，不要带尾斜杠",
               name="默认上游地址"),
    ConfigItem("PROVIDER", "str", "xfyun", group="上游、路由与网络",
               apply=RESTART, description="供应商标签，写入日志与统计记录", name="供应商标签"),
    ConfigItem("TRUST_ENV", "bool", "false", group="上游、路由与网络",
               apply=RESTART, description="是否读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY；国内上游通常建议直连",
               name="信任系统代理"),
    ConfigItem("EXTRA_UPSTREAMS", "str", "", group="上游、路由与网络",
               apply=RESTART, description="额外上游路由：每行一条（前缀 → 上游地址，供应商可选），转发时去掉匹配前缀",
               name="额外上游路由"),

    # ---------------------------------------------------------------- SSE2WS 桥接
    ConfigItem("SSE2WS_MODE", "enum", "off", ("off", "bridge"), group="Codex Responses WebSocket 桥接 (SSE2WS)",
               apply=RESTART, description="开启 /v1/responses WebSocket 入口，供 Codex CLI 等客户端走 WebSocket 长连接",
               name="桥接开关"),
    ConfigItem("SSE2WS_FIRST_EVENT_TIMEOUT", "float", "30", group="Codex Responses WebSocket 桥接 (SSE2WS)",
               apply=RESTART, unit="秒", description="上游已返回响应头但迟迟不发首事件时判定失败的超时（秒）",
               name="首事件超时"),
    ConfigItem("SSE2WS_FIRST_EVENT_RETRIES", "int", "2", group="Codex Responses WebSocket 桥接 (SSE2WS)",
               apply=RESTART, description="首事件超时后整轮重试的次数（0 表示不重试）",
               name="首事件重试次数"),
    ConfigItem("SSE2WS_FIRST_MESSAGE_TIMEOUT", "float", "30", group="Codex Responses WebSocket 桥接 (SSE2WS)",
               apply=RESTART, unit="秒", description="连接建立后等待客户端第一条 response.create 的超时（秒）",
               name="首消息超时"),
    ConfigItem("SSE2WS_INTER_TURN_IDLE_TIMEOUT", "float", "300", group="Codex Responses WebSocket 桥接 (SSE2WS)",
               apply=RESTART, unit="秒", description="轮与轮之间的空闲超时（秒）；超时关闭连接",
               name="轮间空闲超时"),
    ConfigItem("SSE2WS_MAX_BODY_BYTES", "int", "67108864", group="Codex Responses WebSocket 桥接 (SSE2WS)",
               apply=RESTART, description="单轮 response.create 载荷上限（字节）",
               name="单轮载荷上限"),

    # ---------------------------------------------------------------- 连接与响应超时
    ConfigItem("CONNECT_TIMEOUT", "float", "10", group="连接与响应超时",
               apply=RESTART, unit="秒", description="连接上游超时（秒）", name="连接超时"),
    ConfigItem("TIMEOUT", "float", "300", group="连接与响应超时",
               apply=RESTART, unit="秒", description="普通读写超时（秒）；流式响应中表示相邻两次数据之间的最大间隔",
               name="读写超时"),
    ConfigItem("MAX_REQUEST_BODY", "int", "67108864", group="连接与响应超时",
               apply=HOT, description="请求体最大字节数；超出返回 413", name="请求体上限"),
    ConfigItem("RESPONSES_HEADER_TIMEOUT", "float", "120", group="连接与响应超时",
               apply=RESTART, unit="秒", description="整笔 Responses 请求收到响应头的硬上限（秒）；0=不限制",
               name="响应头硬超时"),
    ConfigItem("RESPONSES_ATTEMPT_HEADER_TIMEOUT", "float", "15", group="连接与响应超时",
               apply=RESTART, unit="秒", description="流式 Responses 单个 key 等待响应头的上限（秒）；0=不限制",
               name="单 Key 响应头超时"),
    ConfigItem("RESPONSES_ATTEMPT_HEADER_TIMEOUT_BODY_LIMIT", "int", "1048576",
               group="连接与响应超时", apply=RESTART,
               description="仅对不超过此大小的请求体启用单 key 短超时；0=不限大小",
               name="短超时请求体上限"),

    # ---------------------------------------------------------------- 重试与退避
    ConfigItem("MAX_RETRIES", "int", "60", group="重试与退避",
               apply=HOT, description="最大重试次数；0=无限重试", name="最大重试次数"),
    ConfigItem("RETRY_STATUS_CODES", "csv", "503,502,504,524,529,429", group="重试与退避",
               apply=HOT, description="触发重试的上游状态码，逗号分隔", name="重试状态码"),
    ConfigItem("RETRY_BROAD", "bool", "false", group="重试与退避",
               apply=HOT, description="开启后 5xx、429、401/403 和网络异常均触发重试或换 key",
               name="宽松重试"),
    ConfigItem("RETRY_INTERVAL", "float", "1.0", group="重试与退避",
               apply=HOT, unit="秒", description="非 429 错误的重试间隔（秒）；退避时作为基数", name="重试间隔"),
    ConfigItem("RETRY_BACKOFF", "bool", "false", group="重试与退避",
               apply=HOT, description="非 429 错误开启指数退避", name="指数退避"),
    ConfigItem("RETRY_BACKOFF_MAX", "float", "60", group="重试与退避",
               apply=HOT, unit="秒", description="非 429 指数退避最高等待（秒）", name="退避上限"),
    ConfigItem("RETRY_INTERVAL_429", "float", "5.0", group="重试与退避",
               apply=HOT, unit="秒", description="429 专用重试间隔（秒）；优先尊重 Retry-After",
               name="429 重试间隔"),
    ConfigItem("RETRY_BACKOFF_429", "bool", "true", group="重试与退避",
               apply=HOT, description="429 默认启用指数退避", name="429 指数退避"),
    ConfigItem("RETRY_BACKOFF_MAX_429", "float", "60", group="重试与退避",
               apply=HOT, unit="秒", description="429 指数退避最高等待（秒）", name="429 退避上限"),
    ConfigItem("RETRY_AFTER_MAX", "float", "0", group="重试与退避",
               apply=HOT, unit="秒", description="上游 Retry-After 头封顶值（秒）；0 表示不封顶，完全尊重",
               name="Retry-After 封顶"),

    # ---------------------------------------------------------------- 竞速模式
    ConfigItem("HEDGE_MODE", "enum", "off", ("off", "race", "stagger"), group="竞速模式",
               apply=HOT, description="", name="竞速模式"),
    ConfigItem("MAX_CONCURRENT", "int", "10", group="竞速模式",
               apply=HOT, description="竞速模式单轮最大并发数", name="最大并发数"),

    # ---------------------------------------------------------------- 号池来源与鉴权
    ConfigItem("KEY_AUTH_HEADER", "str", "authorization", group="号池来源与鉴权",
               apply=RESTART, description="自定义上游鉴权头名", name="上游鉴权头"),
    ConfigItem("KEY_AUTH_SCHEME", "str", "Bearer", group="号池来源与鉴权",
               apply=RESTART, description="自定义上游鉴权 scheme；留空表示无 scheme",
               name="上游鉴权 Scheme"),
    ConfigItem("KEY_POOL_FILE", "str", "", group="号池来源与鉴权",
               apply=RESTART, description="CSV 号池文件路径；优先于 KEY_POOLS（compose 默认 /app/key_pool.csv）",
               name="号池 CSV 文件"),
    ConfigItem("KEY_POOLS", "str", "", group="号池来源与鉴权", secret=True, attr="key_pools_raw",
               apply=RESTART, description="号池 Key 列表：Key 用分号分隔；每行一个上游，上游地址与供应商留空表示默认上游",
               name="号池 Key 列表"),

    # ---------------------------------------------------------------- 号池熔断与选择
    ConfigItem("KEY_COOLDOWN", "float", "30", group="号池熔断与选择",
               apply=HOT, unit="秒", description="旧版兼容值；未配置 KEY_COOLDOWN_5XX 时作为默认冷却时间",
               name="默认冷却时间"),
    ConfigItem("KEY_COOLDOWN_5XX", "float", "30", group="号池熔断与选择",
               apply=HOT, unit="秒", description="5xx 错误冷却时间（秒）", name="5xx 冷却时间"),
    ConfigItem("KEY_COOLDOWN_429", "float", "60", group="号池熔断与选择",
               apply=HOT, unit="秒", description="429 错误冷却时间（秒）", name="429 冷却时间"),
    ConfigItem("KEY_COOLDOWN_AUTH", "float", "1800", group="号池熔断与选择",
               apply=HOT, unit="秒", description="鉴权错误冷却时间（秒）", name="鉴权错误冷却"),
    ConfigItem("KEY_COOLDOWN_MAX", "float", "3600", group="号池熔断与选择",
               apply=HOT, unit="秒", description="冷却时间上限（秒）", name="冷却上限"),
    ConfigItem("KEY_COOLDOWN_BACKOFF", "bool", "true", group="号池熔断与选择",
               apply=HOT, description="冷却时间是否指数递增", name="冷却指数递增"),
    ConfigItem("KEY_STICKY", "float", "120", group="号池熔断与选择",
               apply=HOT, unit="秒", description="key 粘性时间（秒）；0=禁用", name="Key 粘性时间"),
    ConfigItem("KEY_POOL_WAIT_TIMEOUT", "float", "120", group="号池熔断与选择",
               apply=HOT, unit="秒", description="所有候选 key 均熔断时的最长等待时间（秒）；超时返回 503，0=不限制",
               name="熔断等待超时"),
    ConfigItem("KEY_TTFT_STALE_AFTER", "float", "300", group="号池熔断与选择",
               apply=HOT, unit="秒", description="真实请求样本有效期（秒）", name="样本有效期"),
    ConfigItem("KEY_TTFT_RETEST_INTERVAL", "float", "60", group="号池熔断与选择",
               apply=HOT, unit="秒", description="便宜分组轮转复测间隔（秒）", name="复测间隔"),
    ConfigItem("KEY_TTFT_CONFIRMATIONS", "int", "2", group="号池熔断与选择",
               apply=HOT, description="慢分组升级所需连续样本数", name="升级确认样本数"),
    ConfigItem("KEY_TTFT_HYSTERESIS", "float", "0.1", group="号池熔断与选择",
               apply=HOT, description="上下阈值的滞回比例", name="滞回比例"),
    ConfigItem("KEY_CACHE_MISS_THRESHOLD", "int", "3", group="号池熔断与选择",
               apply=HOT, description="连续长输入无缓存熔断分组阈值；0=关闭", name="缓存未命中阈值"),
    ConfigItem("KEY_CACHE_MISS_MIN_INPUT_TOKENS", "int", "1024", group="号池熔断与选择",
               apply=HOT, description="触发缓存熔断的最小输入 token 数", name="缓存统计最小输入"),
    ConfigItem("KEY_CACHE_MISS_COOLDOWN", "float", "3600", group="号池熔断与选择",
               apply=HOT, unit="秒", description="缓存熔断冷却时间（秒）；0=关闭", name="缓存熔断冷却"),

    # ---------------------------------------------------------------- 在线同步
    ConfigItem("KEY_POOL_SYNC_DEFAULT_ADAPTER", "str", "sub2api", group="在线同步",
               apply=RESTART, description="在线同步默认适配器", name="同步适配器"),
    ConfigItem("KEY_POOL_SYNC_URL", "str", "UPSTREAM_URL", group="在线同步", attr="key_pool_sync_default_url",
               apply=RESTART, description="管理页新增连接时预填的上游地址；留空时使用 UPSTREAM_URL",
               name="同步默认地址"),
    ConfigItem("KEY_POOL_SYNC_INTERVAL", "int", "300", group="在线同步",
               apply=RESTART, unit="秒", description="自动同步周期（秒）；0=仅手动同步", name="自动同步周期"),
    ConfigItem("KEY_POOL_CREATE_DELAY", "float", "1.5", group="在线同步",
               apply=RESTART, unit="秒", description="同步创建 key 的间隔（秒）", name="建号间隔"),
    ConfigItem("KEY_POOL_SYNC_STATE_FILE", "str", "logs/.key_pool_sync.json", group="在线同步",
               apply=RESTART, description="同步连接与令牌的持久化文件；默认 logs/.key_pool_sync.json",
               name="同步状态文件"),
    ConfigItem("KEY_POOL_SYNC_SECRET", "str", "", group="在线同步", secret=True,
               apply=RESTART, description="加密状态文件凭据；留空回退 ADMIN_PASSWORD",
               name="状态加密密钥"),

    # ---------------------------------------------------------------- Token 统计
    ConfigItem("TOKEN_STATS_INJECT_USAGE", "bool", "false", group="Token 统计",
               apply=HOT, description="为未带 stream_options 的 Chat 流式请求注入 include_usage 以统计 token",
               name="注入用量统计"),

    # ---------------------------------------------------------------- 上游兼容
    ConfigItem("IMAGE_UPSTREAM_USER_AGENT", "str", "", group="上游兼容",
               apply=HOT, description="图片请求覆盖的 User-Agent", name="图片请求 UA"),
    ConfigItem("IMAGE_UPSTREAM_ORIGINATOR", "str", "", group="上游兼容",
               apply=HOT, description="图片请求覆盖的 Originator", name="图片请求 Originator"),

    # ---------------------------------------------------------------- 日志
    ConfigItem("LOG_DIR", "str", "logs", group="日志",
               apply=RESTART, description="日志目录（compose 默认 /app/logs）", name="日志目录"),
    ConfigItem("LOG_RETENTION_DAYS", "int", "30", group="日志",
               apply=RESTART, unit="天", description="日志保留天数", name="日志保留天数"),
    ConfigItem("LOG_LEVEL", "enum", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR"), group="日志",
               apply=RESTART, description="控制台日志级别", name="日志级别"),
    ConfigItem("LOG_FILE", "str", "retry_log.jsonl", group="日志", attr="legacy_log_file",
               apply=RESTART, description="旧版单文件日志路径，仅用于自动迁移", name="旧版日志文件"),

    # ---------------------------------------------------------------- 请求正文敏感信息防护
    ConfigItem("DLP_MODE", "enum", "off", ("off", "audit", "redact", "block"), group="请求正文敏感信息防护",
               apply=HOT, description="", name="防护模式"),
    ConfigItem("DLP_RULES", "csv",
               "private_key,ai_tokens,code_tokens,cloud_tokens,saas_tokens,package_tokens,"
               "credentials,csv_credentials,jwt,connection_string,id_card,bank_card,structured_secret",
               group="请求正文敏感信息防护", apply=HOT, description="启用的敏感信息规则，逗号分隔",
               name="启用规则"),
    ConfigItem("DLP_RULE_FILE", "str", "retry_proxy/dlp_rules.yaml", group="请求正文敏感信息防护",
               apply=RESTART, description="自定义 DLP 规则文件（JSON/YAML）；默认 retry_proxy/dlp_rules.yaml",
               name="规则文件"),
    ConfigItem("DLP_ALLOW_EXEMPTIONS", "bool", "false", group="请求正文敏感信息防护",
               apply=HOT, description="是否允许正文使用固定标记跳过检查；不可信客户端必须保持 false",
               name="允许豁免标记"),
    ConfigItem("DLP_EXEMPT_START", "str", "[[ALLOW_SENSITIVE]]", group="请求正文敏感信息防护",
               apply=HOT, description="豁免区间起始标记", name="豁免起始标记"),
    ConfigItem("DLP_EXEMPT_END", "str", "[[/ALLOW_SENSITIVE]]", group="请求正文敏感信息防护",
               apply=HOT, description="豁免区间结束标记", name="豁免结束标记"),
    ConfigItem("DLP_STRIP_EXEMPT_MARKERS", "bool", "true", group="请求正文敏感信息防护",
               apply=HOT, description="转发前是否移除豁免标记", name="转发前移除标记"),
    ConfigItem("DLP_MAX_BODY_BYTES", "int", "16777216", group="请求正文敏感信息防护",
               apply=HOT, description="最大扫描字节数；redact/block 模式下超限返回 413",
               name="最大扫描字节"),
    ConfigItem("DLP_DECODE_DEPTH", "int", "2", group="请求正文敏感信息防护",
               apply=HOT, description="Base64/hex/percent 编码递归扫描层数；0 层表示关闭解码",
               name="解码扫描层数"),
    ConfigItem("DLP_DECODE_MAX_CANDIDATES", "int", "100", group="请求正文敏感信息防护",
               apply=HOT, description="编码解码候选上限", name="解码候选上限"),
    ConfigItem("DLP_DECODE_MAX_BYTES", "int", "1048576", group="请求正文敏感信息防护",
               apply=HOT, description="编码解码扫描字节上限", name="解码扫描字节上限"),
    ConfigItem("DLP_KNOWN_SECRET_MIN_LENGTH", "int", "8", group="请求正文敏感信息防护",
               apply=HOT, description="号池 Key 小于该长度时不做精确内容匹配，避免短值误报",
               name="精确匹配最小长度"),
    ConfigItem("DLP_FAIL_CLOSED", "bool", "false", group="请求正文敏感信息防护",
               apply=HOT, description="非 JSON 或无法解析的正文是否返回 422", name="无法解析时拦截"),
]

CONFIG_ITEMS_BY_KEY: dict[str, ConfigItem] = {item.key: item for item in CONFIG_ITEMS}
