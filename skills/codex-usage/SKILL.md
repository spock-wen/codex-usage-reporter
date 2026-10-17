---
name: codex-usage
description: 统计本地 Codex token、估算费用、只读识别登录邮箱，并在明确授权后自动接入、首次上报和配置每周周报；用于最近七天、上周自然周、多目录去重和每周上报。默认仅生成本地报告，不自动上传、定时运行或部署服务器。
---

# Codex 用量统计与周报

解析本 skill 的实际目录，使用其中 scripts 下的脚本和可运行的 Python 3.10+，不固定某位用户的路径。报告保存在 skill 外的用户指定目录或当前工作区。个人状态与凭据不放进 skill 或源码仓库。

## 默认只在本地统计

以下命令相对于 skill 根；执行时换成实际绝对脚本路径和报告目录。

```sh
python scripts/report.py run --period last-week --no-upload --output-dir <报告目录>
```

`--no-upload` 不联网、不读取上传凭据，也不调用账户身份 RPC。默认 last-week，为北京时间上个自然周，周一 00:00 至本周一 00:00，左闭右开；未指定 --period 也采用此范围。“最近一周”“上周周报”和未指定周期的统计或上报均用 last-week；只有明确要求“最近7天/滚动7天”时使用 --period last7，按执行时刻向前连续七天。

明确要求“本周”时用 --period this-week，范围为北京时间本周一 00:00 至本次采集时刻，左闭右开；默认和每周定时仍为 last-week。

只读取 CODEX_HOME（设置时）或 ~/.codex 这一个根中的 sessions 和 archived_sessions；--codex-home 可另指定单根。不声称自动扫描所有机器或多个日志根。

## 自动接入与显式上报

团队主地址为 https://codex.wenspock.site，采用**内部自报 · 邮箱未验证**。新装且未配置时默认接入此地址；旧地址 https://usage.wenspock.site 继续提供相同接口，已有配置继续使用原地址和凭据，不静默迁移。完整 skill 目录含 SKILL.md、agents 和 scripts 即可独立使用，不必克隆项目仓库。用户明确要求安装并自动接入时，安装完成后直接运行实际 skill 目录中的 scripts/enrollment.py；已安装 skill 的首次明确上报缺少配置时会自动接入，无需输入邮箱、手签上传令牌、邮件验证码或管理员预登记。

自动接入只读本机 Codex app-server 的当前登录邮箱，使用本机独立设备标识和已持久保存的注册秘密向配置服务取得个人上传凭据。每台机器自行注册，同邮箱多机分别持有凭据；不复制 device-id、注册秘密或其他机器的凭据目录。注册重试保持原设备和原注册记录，新机器不会使其他机器或旧版凭据失效。

用户明确要求单独接入时执行 `python scripts/enrollment.py`，默认使用现有配置地址或团队地址，可用 --url/--config/--codex-home 指定明确目标与本地根，不要求邮箱或令牌输入。仅要求安装 skill 不代表授权接入或上报；安装本身不调用账户身份 RPC、不注册、不上传，也不创建定时任务。

自动配置在后续上报前只读核对当前登录邮箱；换号时拒绝继续上报，不能悄悄把旧配置归属改成新邮箱。API Key 登录、未登录或无邮箱时，提示用户通过正常 Codex 流程登录已有账户；不自动登录、刷新、退出，不直接读取 auth 文件，不从系统用户名猜邮箱。

只有用户明确确认切换上报归属时才重新执行 enrollment.py；历史日志仍不能自动按账户分账，不以重新注册绕过该限制。

邮箱是未验证的内部自报标签，不是可信 SSO 身份，不能证明邮箱所有权或历史日志归属。身份快照只表示当前状态，不把历史全部 token 自动归给该账户。不采集工号、姓名或部门。

只有用户明确要求“采集并上报”时执行：

```sh
python scripts/report.py run --period last-week --output-dir <报告目录>
```

last-week 上报上个自然周。例如 2026-10-19（周一）执行时覆盖 2026-10-12 00:00 至 2026-10-19 00:00（北京时间，不含结束时刻）；日期只是示例。配置存在或 skill 自动发现不代表上传或定时授权。

## 已授权的首次上报与每周自动上报

当用户明确要求“安装后立即上报上周和本周，并设置每周一北京时间 10:00 上报”或同等意图时，安装、自动接入、首次两周期上传和创建该周期任务已获授权，完成以下流程，不再要求用户手动执行命令。若只授权部分动作，仅执行对应部分：

1. 安装完整 skill 后，以实际绝对脚本路径运行 scripts/enrollment.py；已有配置保留地址、设备和私人状态。
2. 先读取并冻结一次当前截止时刻，格式为带时区的 ISO 时间；立即分别执行 scripts/report.py run --period last-week --now <同一截止时刻> --output-dir <skill 外的报告目录> 和 scripts/report.py run --period this-week --now <同一截止时刻> --output-dir <同一报告目录>，完成上周与本周两次采集和上报。两次共用 --now，确保即使执行期间跨周，区间仍连续且互不重叠。两个周期分别保存报告、事件包并核对各自回执；首次和普通手动上报默认不随机延迟。任一上传失败时保留其原包以便重传，分别说明结果，不把失败报告为成功。本周范围结束于冻结的截止时刻，不包含未来记录；周一精确 00:00 尚无正长度本周区间，程序返回 current_week_not_started，不上传空区间包，也不虚构未来微秒；说明稍后可补报本周。
3. 使用当前 Codex 的原生自动化能力，建立或更新本机的“Codex 用量周报”，时间为每周一 10:00、Asia/Shanghai。先检查本机同用途任务并复用，更新不会重复创建；不直接手写内部任务配置，也不替换成操作系统计划任务。记录实际自动化 ID、启用状态和按北京时间核对的下次执行时间；创建失败或当前环境没有原生能力时说明未完成，不声称已定时。

自动化任务提示应指向已安装 skill 的绝对路径、实际 Python 解释器及稳定的 skill 外报告目录，明确使用 last-week 和 --max-upload-delay-seconds 600；每次运行先固定上周范围、采集保存同一包，再在上传前随机错开 0–600 秒，减少团队同时上传的带宽竞争。只有定时上报使用此延迟，纯本地 --no-upload 即使带此参数也不延迟。

```sh
python scripts/report.py run --period last-week --max-upload-delay-seconds 600 --output-dir <报告目录>
```

定时提示同时说明：沿用本机账户和既有配置，仅上传去重事件包；网络失败重传已保存包，跨周不重新采集冒充旧包重试；报告实际区间及 received/new/duplicates/conflicts，身份变化或鉴权失败停止并提示处理，不显示秘密。用量记录依赖本机，执行时需机器开机且 Codex 桌面应用运行；[官方定时任务说明](https://learn.chatgpt.com/docs/automations)。每台需要覆盖的机器各自接入并建立一份本机任务，服务器仍按邮箱汇总与全局请求 ID 去重。

只要求安装、接入或一次上报时，不扩大为定时任务；未授权上传时继续使用 --no-upload。用户之后明确修改或停用周期时，更新同一原生自动化。

脚本一次扫描，保存聚合报告与安全事件包，并将同一包最多重试三次。上传默认每次 60 秒，client.py --timeout 可明确设为最高 120 秒，上传调用总限时 370 秒，不含上传前错峰；注册仍为每次 15 秒、总限时 55 秒。核对 ACK 的 batch_id 和 received 数量。网络失败后使用原文件重传，不重新采集冒充重试：

```sh
python scripts/client.py --events <已保存的events.json>
```

跨周重新运行 last-week 会切换周期。历史补报应固定日期重新采集，再向同一已授权地址提交；以下日期仅为格式示例：

```sh
python scripts/collector.py collect --start 2026-10-12T00:00:00+08:00 --end 2026-10-19T00:00:00+08:00 --output <summary.json> --events-output <events.json>
python scripts/client.py --events <events.json>
```

读取 received/new/duplicates/conflicts/rejected：new=0、duplicates 增加通常表示去重成功，不等于失败；冲突需管理员核对，不能当作已计入统计。401/403 不打印密钥或错误原文，不自动改服务器。客户端不跟随重定向，远端必须 HTTPS；loopback 可用 HTTP。可选 connect_ip 仍保留原域名 Host/SNI、默认 CA 和主机名校验，不能关闭验证或自动修改代理/hosts。

## 私人状态和兼容

状态保存在 CODEX_HOME/codex-usage，未设置时为 ~/.codex/codex-usage，包括 remote 配置、设备标识、注册记录和上传凭据；重试和更新应保留这些文件，不显示注册秘密或上传令牌。旧的手动 configure/token 配置继续兼容，但不是默认接入流程。更换管理员持久密钥可能影响同一注册记录的凭据重建，不能为修部署随意轮换该密钥。

需要单独读取账户时执行 scripts/identity.py account，默认掩码邮箱；完整邮箱仅在用户需要时加 --include-email。只用返回白名单字段，不输出 RPC 错误或认证原文。

## 统计与价格口径

- 只计有稳定 response_id 的 canonical token_usage_record；旧 token_count 和缺少 ID 的记录排除并披露。请求数不等于提问次数。
- 事件 ID 为 sha256(UTF-8('openai:codex:' + response_id))，不加入设备、邮箱、路径、周期或 salt。纯聚合不含事件 ID；事件包含可跨包关联的不可逆 hash，不含原始 response/session ID、对话、日志路径或登录凭据。
- 团队按事件 ID 全局唯一：同机重传、跨机复制与重叠周期不重复，独立新请求累加。相同 ID 的 token、时间、模型或邮箱冲突则隔离排除，不偏向先上传者。批次 ID 不能代替逐请求去重。
- input 包含缓存读写，普通输入为 input 减 cached 和 cache-write；output 包含 reasoning，不重复相加。总量为 input 加 output。未记录的缓存写入不能补猜。
- 本地使用 scripts/prices.json 快照，服务端按自己的价格表重算；披露来源、核验日期、pricing_sha256 和可能过期的限制。需要最新价或新增模型时查官方定价，不套用相近模型价格。
- 未知模型保留 token，显示未定价和已定价小计。费用是 API 等值估算，不是订阅账单或剩余额度；不能用 token 换算额度。

完成时说明实际区间、token 分项、估算或已定价小计、本地文件，以及上报回执（若已授权）。读取 quality/data_status/is_complete/coverage_warnings，披露排除、冲突、未定价和覆盖范围。空包或缺少 canonical 记录不能称真实零；其他设备、云端和已删除历史可能缺失，不声称完整账户用量。
