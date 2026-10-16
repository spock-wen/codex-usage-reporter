# Codex Usage Reporter

从本地 Codex 日志统计 token 和 API 等值费用，自动读取登录邮箱接入团队服务，在网页查看邮箱、设备、每日和模型分布。当前部署地址：[usage.wenspock.site](https://usage.wenspock.site)。采用**内部自报 · 邮箱未验证**，无需管理员预登记、手填邮箱/上传令牌或邮件验证码；看板查看仍需管理员密钥。

同事首次使用请看 [安装与每周一上报指南](docs/colleague-guide.md)。私仓权限就绪后，用 `python scripts/manage_skill.py install --enroll` 安装并自动接入；每周一明确要求 `$codex-usage` 上报上周即可。自包含 skill 位于 [skills/codex-usage](skills/codex-usage/SKILL.md)，默认提示仅本地统计，不隐式上传或创建定时任务。

## 组成

- `collector.py`：读取单个 Codex 日志根中的 `sessions`、`archived_sessions`，生成聚合报告和可选的请求 hash 事件包。详细口径见 [采集器说明](README-collector.md)。
- `identity.py`：通过本地 Codex app-server 只读查询当前登录邮箱和套餐，默认掩码邮箱；不登录、不刷新凭据，也不能据此判定历史请求的账户归属。
- `enrollment.py`：读取本机 Codex 登录邮箱、自动注册本设备凭据并保存私人状态；不使用 OpenAI 登录令牌上报。`client.py` 上传保存的事件包，采用 HTTPS、有界重试和严格 ACK 校验，禁止重定向；`report.py` 串起接入、采集、保存和显式上报。
- `server.py`：Python HTTP + SQLite，匿名接收内部自报注册，再按邮箱凭据接收请求并重算费用；管理员查询统计、隔离冲突和导出 CSV。旧手动签发 API 兼容保留。
- `web/`：中文看板，展示邮箱汇总、趋势、模型、设备同步、未提交及隔离冲突。
- `deploy/`：独立 systemd、Nginx HTTPS/ACME 配置和保留数据与凭据的版本安装脚本，见 [远端部署说明](README-deploy.md)。

## 本地运行

Python 3.10+，运行时只使用标准库，无需安装 Python 第三方依赖。先选择可用的 Python 解释器；Windows Store 占位程序不能执行本项目。

```sh
# 只生成本地报告和安全事件文件，不读取上传令牌、不联网。
python report.py run --period last7 --no-upload --output-dir reports/local

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

请求标识为 `sha256('openai:codex:' + response_id)`，不包含邮箱、机器、路径或时间窗口。服务器使用团队内全局唯一请求键：同机重复上报、跨机复制日志和重叠周期不会重复计算；多台机器的独立请求正常累加。批次 ID 用于识别重试，不能代替逐请求去重。

同一请求标识出现 token、时间、模型或上报邮箱冲突时，进入隔离状态，所有副本从有效统计中排除，不默认先上传者正确。格式非法的事件包整批拒绝。邮箱为未验证的内部自报标签，不是可信 SSO 身份，也不验证历史日志归属。自动配置每次上报前核对当前登录邮箱，换号时拒绝继续，只有明确确认切换归属才重新接入；历史日志仍不自动分账。

输入 token 已包含缓存读写，输出 token 已包含 reasoning，不能重复相加。费用由服务器按 `prices.json` 的模型、缓存和长上下文规则计算，是价格快照下的 API 等值估算，不能解释为订阅扣费或剩余额度。未知模型显示未定价和已定价小计；缺少数据、未提交或冲突时不能把部分观测值称为完整账户用量。

`last7` 为截至执行时刻连续七天；`last-week` 为北京时间上个自然周。所有查询区间左闭右开。日志仅覆盖已读取并上报的本地记录，其他机器、云端、已删除历史及缺少稳定请求 ID 的记录可能不在统计中。

## 仓库边界

维护者修改根目录的 collector、client、configure 等共享脚本或价格文件后，将受管理文件同步进分发包并检查一致性：

```sh
python scripts/manage_skill.py sync
python scripts/manage_skill.py sync --check
python -m unittest discover -v
```

`skills/codex-usage/scripts/` 是分发副本，不应单独修改造成与根源码分叉。成员更新仓库后运行 `python scripts/manage_skill.py install`，安装器保留私人状态和设备 ID；替换文件的备份路径在其 JSON `backup` 字段中，位于仓库 `.local/skill-install-backups/`。

源码、测试和部署模板可以提交；`.local/`、`reports/`、数据库、个人连接配置、上传令牌、管理员密钥、OpenAI 凭据及 TLS 私钥不得提交。测试中的邮箱和请求数据为合成样例。当前版本的实际部署状态与限制以 [部署说明](README-deploy.md) 为准。
