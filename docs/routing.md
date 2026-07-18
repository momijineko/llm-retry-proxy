# 多上游路由

[返回 README](../README.md)

通过 `EXTRA_UPSTREAMS` 可在同一个代理实例内同时转发多个上游端点，按**请求路径前缀**分流。匹配前缀的请求会**去掉前缀**后转发到对应上游；未匹配任何前缀的请求走默认 `UPSTREAM_URL`。

## 配置格式

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
