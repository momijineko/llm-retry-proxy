# 日志、DLP 与记录格式

[返回 README](../README.md)

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
- **敏感信息防误传**：可在转发前检测凭据、私钥、身份证和银行卡；支持仅告警或直接拦截。
- **探测请求不重试**：没有 `model` 的根路径、模型列表和连通性检查只请求上游一次，原样返回状态。
- **旧格式迁移**：首次启动若检测到旧的单文件 `retry_log.jsonl`，自动按日期拆分到 `logs/` 并重建累计汇总，旧文件重命名为 `.bak`。

## 敏感信息拦截与主动豁免

在 `.env` 中启用脱敏后继续转发：

```env
DLP_MODE=redact
DLP_RULES=private_key,ai_tokens,code_tokens,cloud_tokens,saas_tokens,package_tokens,credentials,csv_credentials,jwt,connection_string,id_card,bank_card,structured_secret
DLP_MAX_BODY_BYTES=16777216
```

`redact` 会把未豁免的敏感信息替换为 `[REDACTED:规则名]`，让 Agent 和用户根据上下文决定下一步，不会因普通命中中断调用链。也可以使用 `audit` 仅告警，或使用 `block` 返回 HTTP 422 并停止转发。默认最多扫描 16 MiB 请求体；`redact` 或 `block` 模式下超限会返回 HTTP 413。

DLP 默认递归检查两层 Base64/Base64URL、hex 和 percent 编码；解码后的内容命中时处理整个原始编码片段。候选数量、累计解码字节数和递归深度分别由 `DLP_DECODE_MAX_CANDIDATES`、`DLP_DECODE_MAX_BYTES` 和 `DLP_DECODE_DEPTH` 限制，避免超长或嵌套输入耗尽资源。`redact`/`block` 模式下解码预算耗尽会返回 HTTP 413，避免攻击者用伪候选挤掉真实秘密。启用号池时还会在内存中精确匹配当前 Key，未知厂商格式也能被识别；日志只记录 `known_secret`，不会记录 Key 值。

对于 Chat/Responses/Anthropic 风格请求，DLP 每次都会处理所有用户消息和工具输出，确保本地文件、MCP、Shell 等工具返回的凭据也会在转发副本中脱敏；system/developer 指令、assistant 内容和 JSON Schema 不参与扫描。无法识别结构的通用 JSON 请求会回退到递归扫描全部字符串。

检测规则集中维护在带注释的 `retry_proxy/dlp_rules.yaml`。该文件说明了正则、标志、校验器和敏感 JSON 字段名的配置方式；需要定制时可以直接编辑，或通过 `DLP_RULE_FILE` 指向另一份 YAML/JSON 规则文件。规则文件会在启用 DLP 时随服务启动校验，格式或正则错误会阻止服务启动，避免静默失去防护。

v2 规则支持 `keywords`、`min_entropy`、`validator`、`action`、`placeholder`、`allowlist`、`max_matches`、`secret_group` 和 `enabled`。`secret_group` 可用完整行确认上下文、只替换捕获组，例如 CSV 的 key 列。修改后可以先验证：

```bash
python -m retry_proxy.dlp validate
```

主动豁免默认关闭。只有可信单用户部署确实需要时，先设置 `DLP_ALLOW_EXEMPTIONS=true`，再将确定需要发送的敏感内容包在豁免标记内：

```text
[[ALLOW_SENSITIVE]]需要主动发送的敏感内容[[/ALLOW_SENSITIVE]]
```

启用后该区间跳过检测，标记在请求上游前移除。未配对或嵌套的标记按普通正文处理，不产生豁免，也不会中断 Agent 调用链。日志只记录命中规则和豁免数量，不记录请求正文。固定标记仅适用于可信个人部署，不应作为不可信下游的访问控制机制。

## 字段说明

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
| `retry_codes` | 重试过程中上游返回的错误码列表，如 `[503, 503, 429]`。无重试时为空数组 `[]` |
| `key_id` | 号池模式下使用的 key 标识。配置 `sort` 时为 `label|sort`，否则为 `label`；未设 label 时使用 key 前 8 字符 |
| `key_pool` | 号池模式下实际使用的号池标识（当前为对应上游 URL），未启用号池时为空字符串 |
| `key_attempts` | 号池模式下逐次完成的 key 尝试及可用性判定。触发换 key 的响应记为 `false`，正常响应记为 `true`，主机级连接故障记为 `null` 并从 key 可用率中排除 |

示例：

```json
{"ts":"2026-07-07T11:52:35.123","method":"POST","path":"/chat/completions","provider":"xfyun","model":"spark-v4","upstream_status":200,"final_status":200,"attempts":3,"retries":2,"duration_s":0.852,"succeeded":true,"retry_codes":[503,503],"key_pool":"https://example.com/v2","key_id":"premium|0.2","key_attempts":[{"key_id":"cheap|0.02","available":false},{"key_id":"normal|0.1","available":false},{"key_id":"premium|0.2","available":true}]}
```

快速分析示例：

```bash
# 各模型重试次数统计（当天）
jq -s 'group_by(.model) | map({model:.[0].model, n:length, avg_retries:(map(.retries)|add/length)})' logs/retry_$(date +%Y-%m-%d).jsonl

# 用 pandas
python -c "import pandas as pd; df=pd.read_json('logs/retry_$(date +%Y-%m-%d).jsonl',lines=True); print(df.groupby('model')['retries'].agg(['count','mean','max']))"
```
