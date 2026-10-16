# 同事安装与每周上报

服务地址：[usage.wenspock.site](https://usage.wenspock.site)。采用 **内部自报 · 邮箱未验证**：自动读取本人机器的 Codex 登录邮箱接入，无需填写邮箱、输入上传令牌、邮件验证码或请管理员预登记。仓库已按 [MIT 许可证](../LICENSE) 开源，安装不需要 GitHub 账号或私仓权限；需已正常登录 Codex。

## 一句话交给 Codex 安装

在每台要统计的机器上，把下面的话复制给 Codex：

> 请用 skill-installer 安装 https://github.com/spock-wen/codex-usage-reporter/tree/main/skills/codex-usage ，安装后运行该 skill 的 scripts/enrollment.py，自动读取本机 Codex 邮箱接入 https://usage.wenspock.site 并保存凭证；无需我手填邮箱或令牌，暂不上传历史数据、不创建定时任务；已有安装请安全更新并保留私人配置。

你只需发送这段话，下载 skill、查找 Python 3.10+、执行接入和本地自测都由 Codex 完成，无需本人拉仓库或手动运行命令。公开目录可直接下载，不要求先登录 GitHub。若环境确实缺少可用 Python，Codex 应说明缺少的运行条件，不能声称安装自测完成。

API Key 登录、Codex 未登录或当前账户没有邮箱时，通过 Codex 正常流程登录已有账户后重试，不从系统用户名猜邮箱。安装接入只提交邮箱和本设备注册信息，不上传历史用量；本地自测的 --no-upload 也不联网。

给执行安装的 Codex：先使用 skill-installer 的 GitHub URL 安装方式，仅安装上述自包含目录，记录实际目标目录，再用其中 scripts/enrollment.py 完成接入，运行 scripts/report.py run --period last7 --no-upload --output-dir <skill目录外的本地报告目录> 自测并报告结果。不直接读取 OpenAI 认证文件，不打印注册秘密或凭据，不自动登录或修改代理/hosts。不要假设仓库根目录或 manage_skill.py 已下载。若已有同名 skill，skill-installer 不会原位覆盖：先安装到临时目录，比较差异，将旧版备份到 skill 发现目录之外，再安全更新已存在的唯一安装目录；保留未知文件和所有私人状态，不通过删除整个目录解决冲突。新旧发现目录都有同名 skill 时先确认实际使用位置，避免产生第三份。

安装后的 skill 在下一轮对话可用；如果仍未发现，再重启 Codex 检查实际安装位置。接入和自测可直接使用已记录的绝对脚本路径完成，不依赖当前对话立即发现新 skill。

## 可选：自行拉取仓库安装

如果希望手动维护源码，可以匿名克隆公开仓库：

```sh
git clone https://github.com/spock-wen/codex-usage-reporter.git
```

进入仓库，执行：

```sh
python scripts/manage_skill.py install --enroll
```

需要能实际运行的 Python 3.10+；Linux/macOS 可将示例中的 python 换为 python3，Windows Store 占位程序不能使用。安装器默认 ~/.agents/skills/codex-usage；如果只有旧的 ~/.codex/skills/codex-usage，则更新旧位置；同时存在两份时先核实，再用 --dest 显式选择唯一一份。输出 JSON 中的 destination 是实际 skill 目录，后续脚本使用此路径。

自动接入只进行本地账户只读查询和服务注册，不上传用量。官方发现规则见 [Build skills](https://learn.chatgpt.com/docs/build-skills)。也可复制仓库内完整 skills/codex-usage（含 scripts）到一个用户 skill 目录；不要重复安装到新旧两个目录。

安装后只做本地自测，以下用安装器返回的真实目录替换占位路径。

PowerShell：

```powershell
$SkillDir = '安装器JSON中destination的完整目录'
python (Join-Path $SkillDir 'scripts/report.py') run --period last7 --no-upload --output-dir './reports/local-check'
```

Linux/macOS：

```sh
SKILL_DIR='/替换为安装器返回的完整skill目录'
python "$SKILL_DIR/scripts/report.py" run --period last7 --no-upload --output-dir ./reports/local-check
```

已安装 skill 但尚未接入时，首次明确要求上报会自动完成接入；纯本地 --no-upload 不联网，也不会查询登录身份。

需要单独执行自动接入时可运行 `python "$SKILL_DIR/scripts/enrollment.py"`（PowerShell 用 `python (Join-Path $SkillDir 'scripts/enrollment.py')`）。它只读取当前 Codex 登录邮箱并注册，不需要手填邮箱或令牌。

## 每周一上报上周

每台需要统计的机器在周一向 Codex 明确发送：

> 使用 $codex-usage 采集并上报上周自然周的 Codex 用量到团队服务，保存本地报告，并告诉我时间范围及新增、重复、冲突数量。

也可直接执行：

```sh
python "$SKILL_DIR/scripts/report.py" run --period last-week --output-dir ./reports/weekly
```

PowerShell 使用：

```powershell
python (Join-Path $SkillDir 'scripts/report.py') run --period last-week --output-dir './reports/weekly'
```

last-week 固定按北京时间：上周一 00:00 至本周一 00:00，不含结束时刻。例如 2026-10-19（周一）覆盖 2026-10-12 00:00 至 2026-10-19 00:00，只是日期示例。last7 是执行时刻向前七天，不替代自然周周报。

这是每周一**手动执行**的流程。skill 不会自行每周运行；需要自动化时，由每位成员在本人机器另行明确配置，本次不安装计划任务。

## 多机、重试和补报

同一邮箱的每台机器各自注册并保存设备 ID、注册记录和上传凭据，新机不会使其他机器或旧版令牌失效。不要复制另一台机器的设备 ID、注册秘密、凭据目录。服务器按邮箱汇总，独立请求累加，同一请求的重复上报、复制日志和重叠周期只计算一次。

采集先保存报告与 *-events.json，再提交同一包并有界重试。网络失败后重传原文件：

```sh
python "$SKILL_DIR/scripts/client.py" --events /替换为已保存的events.json
```

跨周再运行 last-week 会切换区间，不能代替旧周重传。旧包还在就直接重传；需要重新采集历史周时固定起止日期（以下仅为示例）：

```sh
python "$SKILL_DIR/scripts/collector.py" collect --start 2026-10-12T00:00:00+08:00 --end 2026-10-19T00:00:00+08:00 --output ./reports/backfill-summary.json --events-output ./reports/backfill-events.json
python "$SKILL_DIR/scripts/client.py" --events ./reports/backfill-events.json
```

回执 new=0 且 duplicates 与包内请求数一致通常是去重成功，不是失败。conflicts 表示同一请求的内容或邮箱归属不一致，已隔离排除，需要管理员核对；不要不断重报试图消除冲突。401/403 或注册冲突保留本机状态并联系维护者，不打印凭据。

自动配置在上报前核对本机当前登录邮箱；换号时拒绝继续使用旧归属，不能静默改名。确认确需切换上报归属后，明确重新运行 enrollment.py；这不会自动把历史日志按账户分开，不能为了绕过保护直接重试上传。邮箱是未验证的自报标签，不是可信 SSO 身份，也不能证明历史日志属于当前账户。未知模型、日志缺失、冲突和其他未同步机器必须保留覆盖限制；费用为 API 等值估算，不是订阅账单。

## 更新与私人状态

普通成员将前面的“一句话”再次发送给 Codex，并说明更新现有 skill 即可；不需要自行维护仓库。更新只替换 skill 程序，保留原设备和私人配置，不会因此自动上传。

选择自行维护源码的成员，在原仓库目录执行：

```sh
git pull --ff-only
python scripts/manage_skill.py install --enroll
```

不能快进或有本地修改时先处理差异，不强制重置。安装器只替换受管理 skill 文件，保留私人状态和设备 ID；替换文件备份到仓库 .local/skill-install-backups，具体位置见返回 JSON 的 backup 字段，避免旧 SKILL.md 被重复发现。

私人配置、设备标识、注册记录和上传令牌存于 CODEX_HOME/codex-usage；未设置 CODEX_HOME 时为 ~/.codex/codex-usage。不要把这些文件或真实报告放入仓库、skill 或交给其他人。旧的 configure.py 和管理员手动签发上传令牌仍兼容，但不再是新成员接入的主流程。
