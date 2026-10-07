# 多上游路由

[返回 README](../README.md)

通过 `EXTRA_UPSTREAMS` 可在同一个代理实例内同时转发多个上游端点，按**请求路径前缀**分流。匹配前缀的请求会**去掉前缀**后转发到对应上游；未匹配任何前缀的合法 API 请求走默认 `UPSTREAM_URL`。

管理页面命名空间（如 `/admin`、`/requests`、`/settings`、`/logs`）未匹配的子路径或请求方法在本地返回 404。未匹配本地路由的浏览器页面导航（GET/HEAD 且带 `Sec-Fetch-Mode: navigate` 或 `Accept: text/html`）也返回本地 404，不发送给上游；去掉路由前缀后，只有已支持的 API 路径（Chat、Responses、Messages、Models、Images、Audio、Embeddings、Files 等）可以转发；其他任意路径均返回本地 404，包括 POST 和带上游前缀的未知路径。资源 ID 与资源子操作的有效性仍由上游判断。

## 配置格式

支持转发 Sub2API 倍率查询端点 `/v1/sub2api/billing`（也可带已配置的代理前缀）。该端点仍使用现有鉴权与号池规则；其它未知的 `sub2api` 子路径保持本地 404。

```
EXTRA_UPSTREAMS=prefix|url|provider,prefix|url|provider,...
```

- `prefix`：请求路径前缀（如 `/anthropic`），不带前导斜杠也行
- `url`：对应上游地址（不要带尾斜杠）
- `provider`：写入日志的供应商标签，用于统计面板区分；留空则取 `prefix` 去掉斜杠

多组用逗号分隔，**长前缀优先匹配**（避免短前缀误吞）。

## 示例

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

统计面板的“按供应商”表格会按 `provider` 标签分别统计各上游的可用率/重试次数；“按路径”表格则按原始请求路径（含前缀）聚合。

## 路由与号池能力的关系

多上游路由先按原始请求路径确定唯一上游并去除代理前缀，随后号池才根据剩余路径和模型筛选兼容 Key。例如 `/anthropic/v1/messages` 先命中 `/anthropic` 上游，号池看到的端点是 `v1/messages`。重试期间不会切换到其它 `EXTRA_UPSTREAMS` 上游。

在线同步池带有结构化能力时，Chat、Responses、Messages、Images、Embeddings、Audio 和 Gemini 原生端点只会进入兼容分组。识别到端点但没有兼容分组时返回 403，不会回落到同一池的其它平台；跨上游或跨协议 fallback 仍需通过显式路由与客户端策略配置。
