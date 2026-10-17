# AI 编程用量看板

代码托管：[GitHub](https://github.com/spock-wen/codex-usage-reporter) · [Gitee 镜像](https://gitee.com/hihope-aifoundry/codex-usage-reporter)。GitHub 访问不畅时，可按[同事指南](docs/colleague-guide.md#github-访问不畅时使用-gitee)交给 Codex 从 Gitee 安装。

Codex 从同事本机日志上报，Cursor 由 65 服务器集中按周拉取；网页按来源、邮箱、每日和模型查看团队用量。两种来源的请求数与金额口径分别呈现，避免把 Cursor 记录金额和 Codex API 等值估算混成一笔账。当前看板地址：[codex.wenspock.site](https://codex.wenspock.site)。Codex 接入采用**内部自报 · 邮箱未验证**，无需管理员预登记、手填邮箱/上传令牌或邮件验证码；看板查看仍需管理员密钥。

新装且没有配置时默认使用 codex.wenspock.site。旧地址 https://usage.wenspock.site 保留相同接口与数据，已有配置继续使用原地址和凭据，更新 skill 不会静默迁移；旧地址直接处理上报，不依赖重定向。

**推荐：每位同事在每台需要统计的机器上，把下面整段贴给 Codex，一次完成安装、首次上报和每周自动上报：**

> 请只下载安装 https://gitee.com/hihope-aifoundry/codex-usage-reporter/tree/main/skills/codex-usage 完整 skill 目录（含 agents 和 scripts，共 9 个文件），不要克隆或下载整个仓库；已有安装请备份后安全更新，保留私人配置、凭据、设备 ID 和原上报地址，不重复安装。运行实际安装目录的 scripts/enrollment.py 自动读取 Codex 邮箱并保存私有接入凭证，新装默认 https://codex.wenspock.site；安装成功后先固定同一个带时区的采集截止时刻，立即分别运行 scripts/report.py run --period last-week --now <截止时刻> 和 scripts/report.py run --period this-week --now <截止时刻>，上报相对此时刻的上周及本周截至该时刻的用量；分别保存本地报告并核对两份回执，反馈各自起止时间、token 用量及新增、重复、冲突数量，任一失败需如实说明。再创建或复用更新本机同用途的 Codex 原生自动化，每周一上午 10:00（Asia/Shanghai）使用实际安装目录上报每次执行时的上周自然周用量（last-week），周任务每次重新取当前时刻，不固定首次 --now，并添加 --max-upload-delay-seconds 600 做设备错峰；首次两次上报均不等待。不重复创建任务，不复制其他机器的凭据；完成后告诉我两份首次回执和自动化设置结果。

该话术明确授权首次上传上周、本周两份数据并创建每周任务。首次两次命令传入同一个带时区的 --now，两个周期均相对此截止时刻计算；this-week 为该时刻所在北京时间周的周一 00:00 至截止时刻，避免两次采集跨周导致漏报。两份报告和回执分别保存、核对，任一失败不能称全部成功。每台机器独立注册、独立执行；同邮箱多机新请求累加，同一请求重复上报只计算一次，不能复制凭据。自动化每周一 10:00 仍只上报当次执行时的上周（last-week），不固定首次 --now，上传前随机等待 0–10 分钟错峰；首次两次上报立即执行，均不加错峰等待。

本地日志任务需要电脑开机且 Codex 桌面应用运行；离线不保证自动补报，历史补报应明确起止日期。参见[官方自动化说明](https://learn.chatgpt.com/docs/automations)。实际执行安装和配置后才算接入完成；仅安装 skill 不会自行创建任务。

**仅需手动上报时，用这段话：**

> 使用 $codex-usage 采集并上报上周自然周的 Codex 用量到已配置的团队服务，保存本地报告，并告诉我起止时间、token 用量及新增、重复、冲突数量。

默认统计和上报上周自然周（last-week），北京时间上周一 00:00 至本周一 00:00，不含结束时刻；本周其他日期执行也覆盖同一个上周。只有明确要求“最近 7 天”或指定 last7 时才用滚动七天。只安装不上传、不定时的备选见[同事指南](docs/colleague-guide.md#备选只安装不上传或定时)。

仓库按 [MIT 许可证](LICENSE) 开源。需已正常登录 Codex，并有可运行的 Python 3.10+；脚本和安装操作由 Codex 执行。详见 [安装与每周上报指南](docs/colleague-guide.md)。自包含 skill 位于 [skills/codex-usage](skills/codex-usage/SKILL.md)。

## 组成

- `collector.py`：读取单个 Codex 日志根中的 `sessions`、`archived_sessions`，生成聚合报告和可选的请求 hash 事件包。详细口径见 [采集器说明](README-collector.md)。
- `identity.py`：通过本地 Codex app-server 只读查询当前登录邮箱和套餐，默认掩码邮箱；不登录、不刷新凭据，也不能据此判定历史请求的账户归属。
- `enrollment.py`：读取本机 Codex 登录邮箱、自动注册本设备凭据并保存私人状态；不使用 OpenAI 登录令牌上报。`client.py` 上传保存的事件包，采用 HTTPS、有界重试和严格 ACK 校验，禁止重定向；`report.py` 串起接入、采集、保存和显式上报。
- `server.py`：Python HTTP + SQLite，接收 Codex 自报事件，并接收管理端提交的 Cursor 自然周快照；同一邮箱和周的 Cursor 重采集替换原快照，避免重复累计。
- `cursor_weekly.py`：服务器端 Cursor 周采集器，读取服务器私有账号清单，只提交邮箱、按周汇总的 token、模型、日期和可用的来源金额，不保存或返回 Cursor API 密钥。
- `web/`：中文统一看板，按来源和邮箱展示周用量、模型、趋势、同步状态；Codex 邮箱对应的姓名和部门由管理员维护在服务端人员名册中，记录不放入源码仓库。
- `deploy/`：独立 systemd、Nginx HTTPS/ACME 配置和保留数据与凭据的版本安装脚本，见 [远端部署说明](README-deploy.md)。

## 本地运行

Python 3.10+，运行时只使用标准库，无需安装 Python 第三方依赖。先选择可用的 Python 解释器；Windows Store 占位程序不能执行本项目。

```sh
# 默认周期为上周自然周；只生成本地报告和安全事件文件，不读取上传令牌、不联网。
python report.py run --period last-week --no-upload --output-dir reports/local

# 已正常登录 Codex 时自动接入，无需手填邮箱或令牌。
python enrollment.py

# 一次采集、保存并上报；首次缺配置也会自动接入。
python report.py run --period last-week --output-dir reports/weekly

# 重传先前保存的事件包，无需再次采集。
python client.py --events reports/weekly/SAVED-events.json

# 使用虚构数据和临时目录运行测试。
python -m unittest discover -v
```

私人连接配置、设备 ID、注册记录和上传凭据位于 `$CODEX_HOME/codex-usage/`，未设置时为 `~/.codex/codex-usage/`，不放入源码仓库。每台机器各自接入；同邮箱多机独立请求累加，新机不会踢掉其他机。不要复制另一台机器的设备 ID、注册秘密或凭据目录，管理员密钥也不分发给成员。

需要在本机启动独立服务时，参见 [本地服务与 API](README-local.md)；在服务器安装或更新时，参见 [部署、HTTPS 与验收](README-deploy.md)。默认本地监听 `127.0.0.1:8765`，部署后的后端仅监听服务器回环地址，由 Nginx 提供 HTTPS。

## 去重、费用与覆盖范围

Codex 请求标识为 `sha256('openai:codex:' + response_id)`，不包含邮箱、机器、路径或时间窗口。服务器使用团队内全局唯一请求键：同机重复上报、跨机复制日志和重叠周期不会重复计算；多台机器的独立请求正常累加。Cursor 当前数据源没有可确认的稳定请求 ID，因此按邮箱和自然周保存完整汇总快照；重复采集替换该邮箱该周的值，不对周快照求和。

同一请求标识出现 token、时间、模型或上报邮箱冲突时，进入隔离状态，所有副本从有效统计中排除，不默认先上传者正确。格式非法的事件包整批拒绝。邮箱为未验证的内部自报标签，不是可信 SSO 身份，也不验证历史日志归属。自动配置每次上报前核对当前登录邮箱，换号时拒绝继续，只有明确确认切换归属才重新接入；历史日志仍不自动分账。

Codex 输入 token 已包含缓存读写，输出 token 已包含 reasoning，不能重复相加。Codex 费用由服务器按 `prices.json` 计算，是 API 等值估算。Cursor token 和记录数按返回的源端事件聚合；有 `chargedCents` 的事件即使没有 token/model 也保留金额，缺少 model 时显示为 `unknown`。缺少源金额的 token 事件会标记金额覆盖不完整。该来源金额不代表订阅账单，也不与 Codex 估算金额合并。分页被截断或账号失败时保留上次成功快照并标记采集不完整；延迟旧快照不能覆盖较新快照或恢复已移除账号。

默认 `last-week` 为北京时间上个自然周；显式 `this-week` 为北京时间本周一 00:00 至本次采集时刻，只有明确要求“最近 7 天”或指定 `last7` 时，才使用截至执行时刻连续七天。所有查询区间左闭右开。日志仅覆盖已读取并上报的本地记录，其他机器、云端、已删除历史及缺少稳定请求 ID 的记录可能不在统计中。

## 仓库边界

维护者修改根目录的 collector、client、configure 等共享脚本或价格文件后，将受管理文件同步进分发包并检查一致性：

```sh
python scripts/manage_skill.py sync
python scripts/manage_skill.py sync --check
python -m unittest discover -v
```

`skills/codex-usage/scripts/` 是分发副本，不应单独修改造成与根源码分叉。成员可以把上面的安装话术再次交给 Codex，要求安全更新已安装 skill。维护者或需要手动操作的成员也可更新仓库后运行 `python scripts/manage_skill.py install`，安装器保留私人状态和设备 ID；替换文件的备份路径在其 JSON `backup` 字段中，位于仓库 `.local/skill-install-backups/`。

源码、测试和部署模板可以提交；`.local/`、`reports/`、数据库、个人连接配置、上传令牌、管理员密钥、OpenAI 凭据及 TLS 私钥不得提交。测试中的邮箱和请求数据为合成样例。当前版本的实际部署状态与限制以 [部署说明](README-deploy.md) 为准。
