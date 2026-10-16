# 同事安装与每周上报

每位成员按邮箱登记，不需要工号、姓名或部门。服务器地址为 [usage.wenspock.site](https://usage.wenspock.site)。本指南提供安装和手动周报流程，不会给任何机器创建计划任务，也不会自动发送消息。

## 管理员先准备

管理员给同事 GitHub 私仓 `spock-wen/codex-usage-reporter` 的读取权限，并在看板“＋添加邮箱”登记同事的上报邮箱。个人上传令牌只显示一次，管理员应当场保存为 JSON `{"upload_token":"此处是服务签发的个人令牌"}` 或单独文本文件，再通过受控渠道交给本人。成员使用个人上传令牌，不使用管理员密钥、GitHub token 或 OpenAI 登录令牌；真实令牌、邮箱配置、报告和数据库不提交到仓库。

再次登记同一邮箱会返回 409，不显示或轮换旧令牌。原令牌找不到时联系管理员处理；当前没有自动重发或自助恢复入口，不能通过重复登记取回令牌。

同一位成员的多台机器用同一邮箱令牌，上报按邮箱合并。每台机器独立安装并生成自己的设备 ID；可以复用个人令牌文件，但不要复制 `device-id.txt`，也不要整目录复制另一台机器的 `codex-usage` 配置。独立请求累加，复制到另一台机器的同一请求不会重复计算。

## 在 Codex 中直接交给助手安装

先取得管理员提供的个人令牌文件。将下面的话复制到要使用的那台机器的 Codex，替换示例邮箱和文件路径即可：

> 请为我安装私有仓库 spock-wen/codex-usage-reporter 中的 codex-usage skill。先确认本机有可运行的 Python 3.10+，使用我已有的 GitHub 权限克隆仓库，或更新已存在的对应 checkout，不要重置本地修改。运行仓库 scripts/manage_skill.py install，使用安装器返回的实际 skill 路径；默认用户 skill 目录按当前安装器处理，避免在 .agents 和旧 .codex 下安装两份。我的上报邮箱是 person@example.com，管理员提供的个人上传令牌文件在“此处替换为本机文件路径”。请运行已安装 skill 的 scripts/configure.py，接收地址 https://usage.wenspock.site。不要打印令牌或读取 OpenAI 登录凭据；已有不同配置时先说明，不使用 --replace 强制覆盖。完成后只执行 --no-upload 的本地自测，不立即上报，不安装定时任务，也不更改代理或 hosts。

私仓无权访问或 GitHub 未登录时先解决当前用户的权限，不共享管理员的 GitHub 登录信息。安装配置不会联网注册邮箱，未登记的邮箱需要管理员先签发对应令牌。

## 手动安装命令

已安装 Git/GitHub CLI 并具有私仓权限时，任选一种克隆方式：

```sh
gh repo clone spock-wen/codex-usage-reporter
# 或使用已有的 Git HTTPS/SSH 认证：
git clone https://github.com/spock-wen/codex-usage-reporter.git
```

进入克隆的仓库，使用能实际运行的 Python 3.10+ 执行：

```sh
python scripts/manage_skill.py install
```

Linux/macOS 若解释器命令为 `python3`，在所有示例中相应替换。Windows 应先运行 `python --version` 或选择实际安装的解释器，不能使用不可运行的 Store 占位程序。安装器默认目标为 `~/.agents/skills/codex-usage`；若只有旧的 `~/.codex/skills/codex-usage`，则更新旧位置，避免双份。可显式使用 `--dest <完整skill目录>`。安装器输出 JSON，确认 `ok=true` 并记录 `destination` 中的完整目录，后续脚本从这个目录执行；若新旧位置同时存在，它会拒绝自行选择，此时先核实保留哪份并明确指定 `--dest`，不要再添第三份。

官方技能目录和自动发现规则见 [Build skills](https://learn.chatgpt.com/docs/build-skills)。也可手动将仓库内整个 `skills/codex-usage` 目录复制到唯一一个用户 skill 目录，必须连同 `scripts/` 一起复制；不要同时放入新旧两个位置。安装后若当前聊天尚未发现该技能，先新开聊天确认 `$codex-usage` 可选；仍不显示时重启 Codex，再检查是否安装到了当前用户的正确目录。

PowerShell（替换两处路径和示例邮箱）：

```powershell
$SkillDir = '安装器输出的完整skill目录'
$TokenFile = '管理员提供的个人令牌文件完整路径'
python (Join-Path $SkillDir 'scripts/configure.py') --email person@example.com --token-file $TokenFile --url https://usage.wenspock.site
python (Join-Path $SkillDir 'scripts/report.py') run --period last7 --no-upload --output-dir './reports/local-check'
```

Linux/macOS：

```sh
SKILL_DIR='/替换为安装器输出的完整skill目录'
TOKEN_FILE='/替换为管理员提供的个人令牌文件'
python "$SKILL_DIR/scripts/configure.py" --email person@example.com --token-file "$TOKEN_FILE" --url https://usage.wenspock.site
python "$SKILL_DIR/scripts/report.py" run --period last7 --no-upload --output-dir ./reports/local-check
```

`configure.py` 仅本地配置，不自动注册、不联网。私人文件在 `CODEX_HOME/codex-usage/`，未设置 `CODEX_HOME` 时为 `~/.codex/codex-usage/`。首次写入 `remote.json` 和 `upload-token.json`；后续显式 `--replace` 写入新的 `upload-token-<uuid>.json`，再原子切换 remote.json 的 token_file 引用。旧令牌文件保留，不供其他人使用；客户端始终按当前引用读取，不应猜定某个固定文件名。生成后的 `device-id.txt` 保留。非默认日志根可在配置及采集命令中显式加 `--codex-home`。已存在不同配置时拒绝覆盖；确认确需替换后才使用 `--replace`。替换连接配置仍保留本机设备 ID。

## 更新已安装的 skill

在原仓库目录执行：

```sh
git pull --ff-only
python scripts/manage_skill.py install
```

有本地修改或无法快进时先处理具体差异，不强制重置。安装器只更新受管理的 skill 文件，保留私人配置、上传令牌和本机设备 ID。若替换了已有文件，会备份到仓库 `.local/skill-install-backups/`，具体路径见返回 JSON 的 `backup` 字段；备份放在 skill 发现目录之外，避免旧 SKILL.md 被重复扫描。继续使用返回的 `destination` 路径，不需重新运行 configure 或改设备 ID。更新后若当前聊天仍使用旧技能内容，可新开聊天，必要时重启 Codex。

## 每周一上报上周

在每台需要统计的机器上，周一向 Codex 明确发送：

> 使用 $codex-usage 采集并上报上周自然周的 Codex 用量到已配置的团队接收地址，保存本地报告，并告诉我时间范围及新增、重复、冲突数量。

或直接执行以下命令，脚本目录仍使用安装器输出的实际路径：

```sh
python "$SKILL_DIR/scripts/report.py" run --period last-week --output-dir ./reports/weekly
```

PowerShell 对应为：

```powershell
python (Join-Path $SkillDir 'scripts/report.py') run --period last-week --output-dir './reports/weekly'
```

`last-week` 固定按北京时间计算：上周一 00:00 至本周一 00:00，不含结束时刻。例如 **2026-10-19（周一）** 上报覆盖 **2026-10-12 00:00 至 2026-10-19 00:00**；这是日期示例，并不是工具固定统计范围。`last7` 是执行时刻向前七天，不用于替代自然周周报。

这是每周一**手动执行**的流程；安装 skill 不会让它自动每周运行。确需自动化时，由每位成员在本人机器另行明确配置触发时间、解释器、日志根和失败重试，本次分发不安装任何任务。

## 失败、补报与回执

采集命令先保存汇总与 `*-events.json`，再有界重试同一个事件包。网络失败后重传已保存的 events 文件，不更改它、不重新采集当作重试：

```sh
python "$SKILL_DIR/scripts/client.py" --events /替换为已保存的events.json
```

跨周后再次运行 last-week 会切到新的自然周，因此不能用它重试旧周。若旧包仍在，直接重传；若要重新采集某个历史周，明确指定起止时间，导出新包后再提交（以下日期仅为示例）：

```sh
python "$SKILL_DIR/scripts/collector.py" collect --start 2026-10-12T00:00:00+08:00 --end 2026-10-19T00:00:00+08:00 --output ./reports/backfill-summary.json --events-output ./reports/backfill-events.json
python "$SKILL_DIR/scripts/client.py" --events ./reports/backfill-events.json
```

回执中 `received` 是本包接收数，`new` 是新增数，`duplicates` 是已有请求数。`new=0` 且 duplicates 与包内请求数一致通常是重传去重成功，不是上报失败。`conflicts` 表示相同请求 ID 的内容或邮箱归属发生冲突：服务器隔离该请求、排除其有效统计，需要管理员核对，不能靠不断重报解决。401/403 应检查个人令牌和管理员登记的邮箱，不把令牌粘贴到聊天或截图中。

只需要本机查询时始终加 `--no-upload`，这一路径不会联网或读取上传令牌。安全事件包包含不可逆的请求 hash，可跨包关联同一次请求；不含原始请求/会话 ID、提示词、代码或 OAuth 凭据。邮箱仅是管理员绑定的上报标签，当前登录邮箱和套餐快照不能证明全部历史日志的真实账户归属。价格为 API 等值估算，未知模型、缺失日志、冲突或未同步机器须保留相应限制，不能当成完整账户账单。
