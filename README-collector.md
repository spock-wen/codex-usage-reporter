# 本机 Codex 用量采集

Python 3.10+，仅使用标准库。只读取 `$CODEX_HOME/sessions/**/*.jsonl` 与 `archived_sessions/**/*.jsonl`，默认 `~/.codex`。不读取 `auth.json`，不修改日志，不输出聊天内容、账号、原始 response ID 或源日志路径。不连接服务器。

```powershell
python collector.py collect --output reports/last-week.json
python collector.py collect --period last-week --output reports/last-week.json
python collector.py collect --period this-week --output reports/this-week.json
python collector.py collect --period last7 --now 2026-10-16T17:38:34+08:00 --output reports/fixed.json
python collector.py collect --period last-week --output reports/last-week.json --events-output reports/last-week-events.json
python -m unittest test_collector -v
```

未指定 `--period` 时默认 `last-week`，即北京时间上周一 00:00 至本周一 00:00；“最近一周”也按此自然周处理。明确需要本周时用 `--period this-week`，为北京时间本周一 00:00 至 `--now`；周一精确 00:00 无正长度区间，安全拒绝，不产生未来边界或无效空区间包。明确需要最近七天或滚动七天时才用 `--period last7`，为截至 `--now` 的连续 7 天。起点包含，终点不包含。筛选的是 `token_usage_record.timestamp`（响应记录时间）。`--start` / `--end` 可同时覆盖周期，必须包含时区；`--codex-home` 指定单个日志根目录；`--prices` 指定价格文件；`--output -` 输出 JSON 到标准输出。

## 严格计数与去重

- 流式读取 JSONL，只计 canonical `token_usage_record.payload.usage`。`payload.response_id` 是全局请求唯一键，键中不加设备、路径或 session。因此从另一机器复制、从 sessions 移到 archived_sessions、同一请求出现在不同线程时都只计一次。
- 同 ID 的规范化时间、任何 usage 数值或最终日志模型不同，整个 ID 的所有副本均排除。即使某一副本明显有误，也不静默挑 first。`conflicted_response_ids` 和不完整状态会披露，正式 `estimated_cost_usd=null`；有效非冲突记录只是部分观测值，不能当成完整用量或真实零。
- 没有 response_id 的记录明确排除并标记不完整，不构造猜测键。旧的 `event_msg.token_count` 一律不计，只记 `legacy_token_count_events_excluded`，避免累计快照、fork 继承历史与现代请求重复计算。仅有旧日志时本工具报告“无 canonical 用量”，不能据此声称真实用量为零。
- 模型优先取 record/payload 显式 model，再按 response 的 `turn_id` 匹配已出现的 `turn_context.turn_id`。找不到匹配时才回退到前序最近 context.model，同时产生 fallback 诊断和警告。完全缺失为 `unknown`。不读取当前 config 猜历史模型。同 ID 模型冲突包括复制 context 引起的不一致，均排除整个 ID。
- 第一个 `session_meta.id` 标识物理线程；后续继承父 metadata 不覆盖它。response_id 承担请求去重，因此不依赖推测 fork 创建边界。
- tokens 必须为非负整数，拒绝布尔、浮点、负数、缓存超过输入、reasoning 超过输出与矛盾的 total。缺失 input/output 无效；可选缓存/思考/cache-write字段缺失按零处理。任何无效 usage、时间、坏行或不可读文件都会让结果明确不完整。
- `input_tokens` 包含 cached 与 cache-write，普通输入 = input - cached - cache-write；这是本工具对 Codex 规范 usage 的计价约定，不适用于把 cache-write 记在 input 之外的自定义 provider。`reasoning_output_tokens` 是 output 子集。`total_tokens = input_tokens + output_tokens`，不会重复加缓存或思考。
- requests 是去重后的有效响应数，不能等同用户提问次数。零 token 的有效响应仍计为一次响应。

## JSON schema_version=2

顶层：`generated_at`, `as_of`, `period`, `period_start`, `period_end`, `timezone`, `data_status`, `is_complete`, `incomplete_reasons`, `earliest_source_usage_at`, `latest_source_usage_at`, `coverage_warnings`, `totals`, `by_model`, `by_day`, `by_day_model`, `pricing_buckets`, `quality`, `metadata`。

聚合字段：`requests`, `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`, `sessions`, `estimated_cost_usd`, `priced_cost_usd`, `priced_cost_breakdown_usd`, `unpriced_requests`, `is_complete`。成本分项为普通输入 `input`、缓存读 `cached_input`、缓存写 `cache_write`、输出 `output`。

`by_model` 行有 `model`；`by_day` 行有 `day`；`by_day_model` 行有 `day,model`；`pricing_buckets` 行有 `day,model,long_context`。sessions 是每组唯一线程数，跨日期 sessions 不能相加；同响应出现在不同 session 的克隆只保留第一次有效副本的 session 归属，不影响请求数、tokens 和费用。

`quality.in_period_*` 针对报告周期，其他计数覆盖全部扫描日志。`estimated_cost_usd=null` 表示报告不完整或存在未定价请求；`priced_cost_usd` 始终只表示有效、非冲突、已定价子集。报告不完整时，数值 totals 也是部分观测值。未知模型保留 tokens，标记未定价，禁止当零价免费。

`earliest_source_usage_at` / `latest_source_usage_at` 是全部扫描范围内有效 source usage 的最早/最晚响应时间，不限当前周期。如果最早记录晚于周期起点，则以 `source_starts_after_period` 标记覆盖不足；没有文件与没有有效 canonical 记录分别标记 `no_files` / `no_canonical_usage_records`，不得当成真实零。日志最后一条早于截止时间不代表期间没有活动。

`is_complete=true` 仅指可用本地源通过了解析与已知覆盖检查，不代表已证明覆盖完整账户。metadata 含定价配置 SHA256、核验日期和来源、模型策略与口径限制，不含对话或标识。

## 定价

`prices.json` 的 `models` 字典使用每百万 token 的美元价：`input`, `cached_input`, `cache_write`, `output`；可配置 `long_context_threshold`, `long_input_multiplier`, `long_cached_input_multiplier`, `long_cache_write_multiplier`, `long_output_multiplier`。输入严格大于阈值时应用整次请求的倍率。未配置缓存倍率时继承长输入倍率。

费用是指定价格快照的 Standard API 等值估算，不能解释为订阅账单或额度兑换，也不代表历史当日价格。未知模型或实际用到但没有单价的 token 类型保留未定价状态。日志未记录的缓存写入、额外工具费、Fast 等速度档费用无法据此单独核验。

本机日志不保证覆盖其他设备、云端或已删除任务；同一本机切换账号时，本报告不承诺按账号隔离。本工具仅产出本地汇总，不含上传或服务器功能。

## 可选请求级事件包

`--events-output <本地JSON路径>` 在同一遍日志扫描中另存事件包，聚合 `--output` 仍只含汇总、不会加入请求 hash。两个输出文件必须不同；事件包不支持 `-` 标准输出。此选项只创建本地文件，不上传、不注册设备、不连接服务器。

Python 接口保持兼容：`collect(...)` 返回原聚合字典；显式 `collect(..., include_events=True)` 返回 `(report, event_package)` 元组。两份结果来自相同的有效、非冲突、区间内 canonical 请求集，因此包中事件数等于聚合 requests，各 token 字段之和与聚合一致。

事件包 `schema_version=1`、`kind=codex_usage_events`，含 `period_start/end`、`timezone`、`batch_id`、`generated_at`、`data_status`、`is_complete`、`incomplete_reasons`、`quality`、`coverage_warnings`、`metadata` 和 `events`。每条事件只含：

- `event_id`：`sha256(UTF-8('openai:codex:' + response_id))` 的 64 位十六进制值。原 response ID 不输出，不在算法中加入设备、路径、session、成员、邮箱或随机 salt。
- `timestamp`：规范化的 `+08:00` 响应记录时间。
- `model`：按前述日志模型策略得到的模型，缺失为 `unknown`。
- `input_tokens`、`cached_input_tokens`、`cache_write_input_tokens`、`output_tokens`、`reasoning_output_tokens`、`total_tokens`：不含客户端费用，由未来接收端按自己的价格表重算。

事件按 event_id 排序。`batch_id` 是仅对 `{schema_version, period_start, period_end, events}` 进行 `json.dumps(sort_keys=True, ensure_ascii=True, separators=(',', ':'))` 后 UTF-8 编码的 SHA256；不纳入生成时间、诊断数量、扫描路径、当前价格或登录身份。同一固定时间窗口、同一有效请求集重跑时 batch_id 相同，窗口外新日志或扫描诊断变化不改变它。周期变化可产生不同 batch_id，同一请求跨重叠周期的 event_id 始终相同。

未来服务器应以 **team_id + event_id** 建唯一键：同机两次上传、跨机复制日志、重叠窗口重传都只能计一次；不同机器的独立请求 ID 组成并集并累加。不能将 device_id 或 batch_id 当作请求唯一键。相同事件出现不同成员归属或不同 token/模型/时间时，应单列冲突待核对，不能静默分给先上传的人。上述是接收端约定，当前项目没有实现网络上传、ACK、outbox 或服务器去重。

同 ID 冲突及无效请求不会导出。未知模型可导出 token，不伪造费用。没有 canonical 记录时输出空 events 并明确不完整，不能称为账户真实零用量；即使包有事件，也必须保留覆盖警告。只凭当前登录账户不能强绑定历史事件，包中账户归属明确为 unknown。

请求 hash 虽不可逆，仍可跨包关联同一次调用，隐私范围与纯聚合不同；事件包不含原始 response/session ID、源路径、对话或认证信息。仅在用户需要请求级导出时启用，并将文件放在受控的本地目录。
