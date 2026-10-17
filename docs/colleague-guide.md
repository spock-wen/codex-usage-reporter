# 同事安装与每周上报

服务地址：[codex.wenspock.site](https://codex.wenspock.site)。采用 **内部自报 · 邮箱未验证**：自动读取本人机器的 Codex 登录邮箱接入，无需填写邮箱、输入上传令牌、邮件验证码或请管理员预登记。仓库已按 [MIT 许可证](../LICENSE) 开源，安装不需要 GitHub 账号或私仓权限；需已正常登录 Codex。

新装且没有配置时默认使用 codex.wenspock.site。旧地址 https://usage.wenspock.site 保留相同接口与数据，已有配置继续使用原地址和凭据，更新 skill 不会静默迁移；旧地址直接处理上报，不依赖重定向。

## 推荐：安装、立即上报并设置每周自动化

每位同事在**每台需要统计的机器**上，将下面整段交给 Codex，无需本人拉仓库或运行命令：

> 请只下载安装 https://gitee.com/hihope-aifoundry/codex-usage-reporter/tree/main/skills/codex-usage 完整 skill 目录（含 agents 和 scripts，共 9 个文件），不要克隆或下载整个仓库；已有安装请备份后安全更新，保留私人配置、凭据、设备 ID 和原上报地址，不重复安装。运行实际安装目录的 scripts/enrollment.py 自动读取 Codex 邮箱并保存私有接入凭证，新装默认 https://codex.wenspock.site；安装成功后先固定同一个带时区的采集截止时刻，立即分别运行 scripts/report.py run --period last-week --now <截止时刻> 和 scripts/report.py run --period this-week --now <截止时刻>，上报相对此时刻的上周及本周截至该时刻的用量；分别保存本地报告并核对两份回执，反馈各自起止时间、token 用量及新增、重复、冲突数量，任一失败需如实说明。再创建或复用更新本机同用途的 Codex 原生自动化，每周一上午 10:00（Asia/Shanghai）使用实际安装目录上报每次执行时的上周自然周用量（last-week），周任务每次重新取当前时刻，不固定首次 --now，并添加 --max-upload-delay-seconds 600 做设备错峰；首次两次上报均不等待。不重复创建任务，不复制其他机器的凭据；完成后告诉我两份首次回执和自动化设置结果。

这段话同时授权首次上传上周、本周两份数据并创建每周任务。先固定一个带时区的 ISO 截止时刻，两次命令均传相同的 --now <截止时刻>；last-week 与 this-week 都相对此时刻计算，this-week 从该时刻所在北京时间周的周一 00:00 至截止时刻，避免两次采集跨周导致漏报。两次分别使用 skill 外的报告目录，例如 reports/first-last-week 和 reports/first-this-week，并分别核对 ACK 与起止时间、token、新增、重复、冲突；一份失败时保留两份结果，不能声称全部成功。

然后用 Codex 原生自动化保存周一 10:00（Asia/Shanghai）的计划，**周任务仍只执行 last-week，并在每次执行时重新取当前时刻，不能固定首次 --now**；同一机器已有同用途任务时更新复用，不另建一份。运行指令必须使用实际 Python 与 skill 脚本的绝对路径；首次两次均不加延迟参数，只有周任务加 --max-upload-delay-seconds 600。

### 周任务的执行条件与错峰

任务在每周一 10:00 触发，各设备上传前随机等待 0–600 秒错峰；实际开始还受任务排队和采集耗时影响。电脑需开机且 Codex 桌面应用运行，并能访问本机日志与服务；CLI/IDE 环境不支持原生任务管理时，应明确提示改用桌面应用，不声称已经排程。[官方自动化说明](https://learn.chatgpt.com/docs/automations)

离线不保证自动补报。恢复后先核对缺失周期；本周内重新执行 last-week 仍对应同一上周，跨周则改用固定起止日期或重传已保存事件包，不能把新的 last-week 当旧周补报。

以一次本机上周样本估算：7,095 条请求的事件包约 2.13 MB，30 台同规模机器合计约 63.8 MB；仅按 5 Mbps 计算，纯传输约 102 秒，尚未计网络、HTTPS、服务处理与重试开销。按设备数量估算，30 人多机可能超过 30 台；这只是样本线性估算，未经 30 台并发压测，不是性能保证，因此周任务保留 0–10 分钟错峰。

## 备选：每周手动上报

不想创建自动化时，在每台机器完成安装后手动发送：

> 使用 $codex-usage 采集并上报上周自然周的 Codex 用量到已配置的团队服务，保存本地报告，并告诉我起止时间、token 用量及新增、重复、冲突数量。

默认统计和上报上周自然周（last-week），建议每周一执行；本周其他日期执行也覆盖同一个上周。周期固定按北京时间：上周一 00:00 至本周一 00:00，不含结束时刻。例如 2026-10-19（周一）执行覆盖 2026-10-12 00:00 至 2026-10-19 00:00，此处仅为日期示例。只有明确要求“最近 7 天”或指定 last7 时才用滚动七天，不用它替代自然周周报；安装后的本地自测也使用 last-week。

每台机器各自接入并上报，同邮箱的独立请求累加，同一请求重复上传只计算一次，不复制设备 ID 或凭据。需要脚本命令时见[直接运行上报脚本](#可选直接运行上报脚本)。

## 备选：只安装，不上传或定时

只想先安装检查时使用下面这段；它不会上传或创建自动化：

> 请用 skill-installer 安装 https://github.com/spock-wen/codex-usage-reporter/tree/main/skills/codex-usage ，安装后运行该 skill 的 scripts/enrollment.py，自动读取本机 Codex 邮箱接入 https://codex.wenspock.site 并保存凭证；无需我手填邮箱或令牌，暂不上传历史数据、不创建定时任务；已有安装请安全更新并保留私人配置和原上报地址。

## 安装检查与备选方式

你只需发送前面的安装话术，下载 skill、查找 Python 3.10+、执行接入和本地自测都由 Codex 完成，无需本人拉仓库或手动运行命令。公开目录可直接下载，不要求先登录 GitHub。若环境确实缺少可用 Python，Codex 应说明缺少的运行条件，不能声称安装自测完成。

API Key 登录、Codex 未登录或当前账户没有邮箱时，通过 Codex 正常流程登录已有账户后重试，不从系统用户名猜邮箱。安装接入只提交邮箱和本设备注册信息，不上传历史用量；本地自测的 --no-upload 也不联网。

给执行安装的 Codex：使用上面的 GitHub 只安装备选时，按 skill-installer 的 GitHub URL 方式安装自包含目录，记录实际目标目录，再用其中 scripts/enrollment.py 完成接入，运行 scripts/report.py run --period last-week --no-upload --output-dir <skill目录外的本地报告目录> 自测并报告结果；推荐的 Gitee 完整流程按下方匿名下载说明取文件，再首次上报并设置自动化。不直接读取 OpenAI 认证文件，不打印注册秘密或凭据，不自动登录或修改代理/hosts。不要假设仓库根目录或 manage_skill.py 已下载。若已有同名 skill，skill-installer 不会原位覆盖：先安装到临时目录，比较差异，将旧版备份到 skill 发现目录之外，再安全更新已存在的唯一安装目录；保留未知文件和所有私人状态，不通过删除整个目录解决冲突。新旧发现目录都有同名 skill 时先确认实际使用位置，避免产生第三份。

安装后的 skill 在下一轮对话可用；如果仍未发现，再重启 Codex 检查实际安装位置。接入和自测可直接使用已记录的绝对脚本路径完成，不依赖当前对话立即发现新 skill。

### GitHub 访问不畅时使用 Gitee

以下是 Gitee 的**只安装备选**，不包含首次上传或周任务；完整流程请使用文首推荐话术：

> 请只下载安装 https://gitee.com/hihope-aifoundry/codex-usage-reporter/tree/main/skills/codex-usage 这个完整 skill 目录（含 agents 和 scripts），不要克隆或下载整个仓库；已有安装请备份后安全更新，保留私人配置、凭据、设备 ID 和原上报地址，不重复安装。运行实际安装目录的 scripts/enrollment.py 自动读取 Codex 邮箱并保存接入凭证，新装默认 https://codex.wenspock.site；无需我手填邮箱或令牌，暂不上传历史数据、不创建定时任务，完成后做 --no-upload 本地自测。

给执行安装的 Codex：skill-installer 的 URL 安装方式仅支持 GitHub，不能直接传 Gitee URL。Gitee 使用匿名 Contents API，仅取这个目录，不下载整仓归档，也不依赖仓库根的 manage_skill.py：

1. 请求 `https://gitee.com/api/v5/repos/hihope-aifoundry/codex-usage-reporter/branches/main`，先将 main 解析为返回的 `commit.sha`。
2. 使用 `https://gitee.com/api/v5/repos/hihope-aifoundry/codex-usage-reporter/contents/skills/codex-usage?ref=<SHA>` 检查目录，后续所有文件均通过同一个 Contents API、同一个固定 SHA 获取，不再用浮动 main，防止混合版本。
3. 只取以下 9 个相对路径，保留目录结构并确认文件齐全；失败时停止，不把网页 HTML 当脚本，也不回退下载整仓。

```text
SKILL.md
agents/openai.yaml
scripts/collector.py
scripts/identity.py
scripts/client.py
scripts/report.py
scripts/configure.py
scripts/enrollment.py
scripts/prices.json
```

例如读取文件 `scripts/enrollment.py` 时，请求 `.../contents/skills/codex-usage/scripts/enrollment.py?ref=<SHA>`，按响应的 encoding 解码 content。全部文件在临时目录验证完成后再安装，记录实际 skill 目录；新装使用当前 Codex 的用户 skill 目录，已有安装更新原位置。将旧版备份到 skill 发现目录之外，逐文件原子替换受管理文件；保留未知文件以及所有私人配置、凭据、设备 ID、原上报地址，不删除整个原目录，不产生重复安装。若本地程序有修改，先保留差异，不覆盖丢失。

安装后直接用实际目录的 scripts/enrollment.py 接入；只需本地自测则执行 scripts/report.py run --period last-week --no-upload，并把报告放在 skill 目录之外。明确选择推荐流程时，先固定一个带时区截止时刻，分别执行 scripts/report.py run --period last-week --now <截止时刻> --output-dir <上周报告目录> 和 scripts/report.py run --period this-week --now <截止时刻> --output-dir <本周报告目录>，两次使用同一时刻；核对两份回执后设置每次实时计算、只跑 last-week 的周任务。只要求手动上报上周时则仅执行 last-week。以上操作均无需仓库根脚本。

## 维护者/开发者可选：自行拉取仓库

**普通同事无需这一步**；仅希望维护源码或开发的人可以匿名克隆公开仓库：

```sh
git clone https://github.com/spock-wen/codex-usage-reporter.git
```

GitHub 访问不畅时，改用下面这一条，二选一即可：

```sh
git clone https://gitee.com/hihope-aifoundry/codex-usage-reporter.git
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
python (Join-Path $SkillDir 'scripts/report.py') run --period last-week --no-upload --output-dir './reports/local-check'
```

Linux/macOS：

```sh
SKILL_DIR='/替换为安装器返回的完整skill目录'
python "$SKILL_DIR/scripts/report.py" run --period last-week --no-upload --output-dir ./reports/local-check
```

已安装 skill 但尚未接入时，首次明确要求上报会自动完成接入；纯本地 --no-upload 不联网，也不会查询登录身份。

需要单独执行自动接入时可运行 `python "$SKILL_DIR/scripts/enrollment.py"`（PowerShell 用 `python (Join-Path $SkillDir 'scripts/enrollment.py')`）。它只读取当前 Codex 登录邮箱并注册，不需要手填邮箱或令牌。

## 可选：直接运行上报脚本

使用 Codex 安装后记录的实际 skill 目录，也可直接执行：

```sh
python "$SKILL_DIR/scripts/report.py" run --period last-week --output-dir ./reports/weekly
```

PowerShell 使用：

```powershell
python (Join-Path $SkillDir 'scripts/report.py') run --period last-week --output-dir './reports/weekly'
```

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

普通成员将前面的“只安装”话术再次发送给 Codex，并说明更新现有 skill 即可；仍只下载 skill 目录，不需要维护仓库。更新前将旧版备份到 skill 发现目录之外，只替换受管理程序文件，保留未知文件、原设备、私人配置、凭据和原上报地址，不会因此自动上传。不要选择推荐的完整接入话术来做仅更新操作；仅更新不会创建或更改已有自动化。

维护者/开发者选择自行维护源码时，在原仓库目录执行：

```sh
git pull --ff-only
python scripts/manage_skill.py install --enroll
```

不能快进或有本地修改时先处理差异，不强制重置。安装器只替换受管理 skill 文件，保留私人状态和设备 ID；替换文件备份到仓库 .local/skill-install-backups，具体位置见返回 JSON 的 backup 字段，避免旧 SKILL.md 被重复发现。

私人配置、设备标识、注册记录和上传令牌存于 CODEX_HOME/codex-usage；未设置 CODEX_HOME 时为 ~/.codex/codex-usage。不要把这些文件或真实报告放入仓库、skill 或交给其他人。旧的 configure.py 和管理员手动签发上传令牌仍兼容，但不再是新成员接入的主流程。
