---
name: codex-usage
description: 统计本地 Codex token、估算费用、只读识别当前登录邮箱，以及在用户明确授权后上报团队用量；用于最近七天、上周自然周、多目录去重和每周上报请求。默认仅生成本地报告，不自动上传、定时运行或部署服务器。
---

# Codex 用量统计与周报

先解析本 skill 的实际安装目录，使用其中 `scripts/` 下的脚本。需要可运行的 Python 3.10+，不得固定某位用户的解释器路径；Windows Store 占位程序不算可用解释器。报告保存在 skill 外的用户指定目录或当前工作区，个人配置和令牌不写入 skill。

## 本地统计为默认行为

以下命令相对于 skill 根；执行时换成实际绝对脚本路径及输出目录。

```sh
python scripts/report.py run --period last7 --no-upload --output-dir <报告目录>
```

`--no-upload` 不读取远程配置或上传令牌、不联网。`last7` 是执行时刻向前连续七天；`last-week` 是北京时间上个自然周，周一 00:00 至本周一 00:00，左闭右开。“最近一周”默认 last7，“上周周报”用 last-week，不混用两种时间口径。

默认读取 `CODEX_HOME`（设置时）或 `~/.codex` 这一个日志根下的 `sessions`、`archived_sessions`；可用 `--codex-home` 指定另一个根。不声称自动扫描所有机器或任意多个日志根。

需要只读识别当前登录账户时用 `scripts/identity.py account`，默认掩码邮箱；完整邮箱仅在用户需要时加 `--include-email`。通过本地 app-server 读取，不直接解析认证文件，不登录、刷新或退出账户。快照只代表当前登录状态，不能把全部历史用量归给当前账户；API Key、未登录或无邮箱保留相应状态。

## 首次配置

由服务管理员按邮箱签发专属上传令牌；不收集工号、姓名或部门。配置只在本地落盘，不自动注册或连接服务器：

```sh
python scripts/configure.py --email person@example.com --token-file <管理员签发的令牌文件> --url https://usage.wenspock.site
```

令牌文件为接收服务签发的 JSON `{"upload_token":"接收服务签发的令牌"}` 或原始令牌文本；不使用 OpenAI 登录令牌，不在命令行直接写令牌。配置位于 `CODEX_HOME/codex-usage/`，未设置时为 `~/.codex/codex-usage/`：首次令牌为 `upload-token.json`；后续显式替换写入新的 `upload-token-<uuid>.json`，再原子切换 `remote.json` 的 token_file 引用，旧文件保留且不能分发给其他人。已有不同配置应拒绝覆盖；只有用户明确要求替换才用 `--replace`。可用 `--codex-home` 指定实际日志和配置根。令牌遗失联系管理员，当前不支持自动重发或自助恢复；重复登记同邮箱返回 409，不轮换旧令牌。

同一人的多台机器可使用同一邮箱专属上传令牌；每台机器生成并保留自己的 device ID，不复制 `device-id.txt` 或别人的配置目录。邮箱是管理员绑定的上报标签，不能凭它验证历史账户归属。不要共享管理员密钥、其他人的上传令牌或把凭据写入仓库。

## 明确授权后上报

用户明确要求“采集并上报”时，使用已配置地址提交；配置存在本身不代表允许上传。服务地址变更需要用户确认目标，不自动换服务器或发送给其他人。

```sh
python scripts/report.py run --period last-week --output-dir <报告目录>
```

每周一用户手动执行上报上周：例如 **2026-10-19（周一）** 执行 last-week，覆盖 **2026-10-12 00:00 至 2026-10-19 00:00（北京时间，不含结束时刻）**。这只是周期示例，不固定为实际采集日期。skill 不会自行每周运行；设置定时任务须由每位成员在本人机器另行明确配置，本流程不创建任务。

脚本一次扫描，保存聚合报告和安全事件包，将同一包最多重试三次，并核对 ACK 的 batch_id 与 received 数量。失败后保留文件，在同一已授权地址重传保存的包：

```sh
python scripts/client.py --events <已保存的events.json>
```

不要跨周把重新执行 last-week 当作重试。补报指定历史区间时，固定起止时间重新采集再显式提交：

```sh
python scripts/collector.py collect --start 2026-10-12T00:00:00+08:00 --end 2026-10-19T00:00:00+08:00 --output <summary.json> --events-output <events.json>
python scripts/client.py --events <events.json>
```

上述日期同样只是格式示例；按用户需要替换，两个输出路径须不同。读取 received/new/duplicates/conflicts/rejected：`new=0`、duplicates 增加通常表示重传已去重，不等于失败；冲突需管理员核对，不能把冲突当作已计入统计。401/403 检查个人令牌及归属，不打印服务端错误原文或密钥。网络失败不得无限重试。

客户端只向 HTTPS 远端发送令牌，不跟随重定向；loopback 可用 HTTP。可选 connect_ip 仅在用户明确需要时使用合法 IP，TLS Host/SNI 和默认 CA/域名校验仍使用授权域名，不关闭证书验证，不自动修改代理或 hosts。

## 去重与计价口径

- 只计具有稳定 response_id 的 canonical token_usage_record。旧 token_count 和缺少 ID 的记录排除并披露；请求数不等于提问次数。
- 事件 ID 为 `sha256(UTF-8('openai:codex:' + response_id))`，不加入设备、邮箱、路径、周期或 salt。原始 response/session ID、对话、日志路径和认证信息不进入报告。纯聚合报告不含事件 ID；事件包包含可跨包关联的不可逆请求 hash，隐私范围不同。
- 服务器在团队内按事件 ID 唯一入库：同机重传、复制日志与重叠周期不重复；多台机器的独立请求累加。同 ID 的 token、时间、模型或邮箱冲突则隔离排除，不偏向先上传者。批次 ID 不能代替逐请求去重。
- input 包含缓存读写，普通输入为 input 减 cached 和 cache-write；output 包含 reasoning，不能重复相加。总量为 input 加 output。日志未记录的缓存写入不能补猜。
- 本地按 scripts/prices.json 快照估算，服务端按自己的价格表重算，不信客户端费用。披露价格来源、核验日期及 pricing_sha256；快照不是实时价格，历史用量按快照重估。需要最新价或新增模型时查官方公开定价，不能套用相似模型价格。
- 未知模型显示未定价及已定价小计，不能当零费用。金额为 API 等值成本，不是订阅账单或剩余额度。账户额度需独立查询，不能用 token 或费用换算。

完成时报告实际区间、token 分项、估算或已定价小计、本地文件位置，以及上报回执（若授权提交）。读取 quality/data_status/is_complete/coverage_warnings，说明排除、冲突、未定价和覆盖限制。没有 canonical 数据或空包不能宣称真实零；其他设备、云端、已删除历史可能缺失，不能声称完整账户用量。
