# Codex Usage Reporter

从本地 Codex 日志统计 token 和 API 等值费用，按邮箱上报到接收服务，在网页查看成员、设备、每日和模型分布。当前部署地址：[usage.wenspock.site](https://usage.wenspock.site)。看板需要管理员密钥，上报使用各邮箱独立的上传令牌，两者均与 OpenAI 登录凭据分开。

## 组成

- `collector.py`：读取单个 Codex 日志根中的 `sessions`、`archived_sessions`，生成聚合报告和可选的请求 hash 事件包。详细口径见 [采集器说明](README-collector.md)。
- `identity.py`：通过本地 Codex app-server 只读查询当前登录邮箱和套餐，默认掩码邮箱；不登录、不刷新凭据，也不能据此判定历史请求的账户归属。
- `client.py`：上传已保存事件包，使用 HTTPS、有界重试和严格 ACK 校验；禁止重定向。`report.py` 将一次采集、本地保存和可选上报串起来。已安装的 `codex-usage` skill 使用同一组脚本。
- `server.py`：Python HTTP + SQLite，按服务端绑定的邮箱接收请求并重算费用；管理员登记邮箱、查询统计和导出 CSV。
- `web/`：中文看板，展示邮箱汇总、趋势、模型、设备同步、未提交及隔离冲突。
- `deploy/`：独立 systemd、Nginx HTTPS/ACME 配置和保留数据与凭据的版本安装脚本，见 [远端部署说明](README-deploy.md)。

## 本地运行

Python 3.10+，运行时只使用标准库，无需安装 Python 第三方依赖。先选择可用的 Python 解释器；Windows Store 占位程序不能执行本项目。

```sh
# 只生成本地报告和安全事件文件，不读取上传令牌、不联网。
python report.py run --period last7 --no-upload --output-dir reports/local

# 配置个人 remote.json 和上传令牌后，一次采集、保存并上报。
python report.py run --period last-week --output-dir reports/weekly

# 重传先前保存的事件包，无需再次采集。
python client.py --events reports/weekly/SAVED-events.json

# 使用虚构数据和临时目录运行测试。
python -m unittest discover -v
```

个人连接配置默认放在 `$CODEX_HOME/codex-usage/remote.json`，未设置 `CODEX_HOME` 时为 `~/.codex/codex-usage/remote.json`；令牌单独存放并由配置的 `token_file` 引用。服务器地址为 `https://usage.wenspock.site`。成员只能使用自己的上传令牌；管理员密钥不要分发给上报用户。连接配置、令牌和真实报告均不放入源码仓库。

需要在本机启动独立服务时，参见 [本地服务与 API](README-local.md)；在服务器安装或更新时，参见 [部署、HTTPS 与验收](README-deploy.md)。默认本地监听 `127.0.0.1:8765`，部署后的后端仅监听服务器回环地址，由 Nginx 提供 HTTPS。

## 去重、费用与覆盖范围

请求标识为 `sha256('openai:codex:' + response_id)`，不包含邮箱、机器、路径或时间窗口。服务器使用团队内全局唯一请求键：同机重复上报、跨机复制日志和重叠周期不会重复计算；多台机器的独立请求正常累加。批次 ID 用于识别重试，不能代替逐请求去重。

同一请求标识出现 token、时间、模型或上报邮箱冲突时，进入隔离状态，所有副本从有效统计中排除，不默认先上传者正确。格式非法的事件包整批拒绝。邮箱表示管理员绑定的上报归属，不是对历史 OpenAI 账户身份的验证。

输入 token 已包含缓存读写，输出 token 已包含 reasoning，不能重复相加。费用由服务器按 `prices.json` 的模型、缓存和长上下文规则计算，是价格快照下的 API 等值估算，不能解释为订阅扣费或剩余额度。未知模型显示未定价和已定价小计；缺少数据、未提交或冲突时不能把部分观测值称为完整账户用量。

`last7` 为截至执行时刻连续七天；`last-week` 为北京时间上个自然周。所有查询区间左闭右开。日志仅覆盖已读取并上报的本地记录，其他机器、云端、已删除历史及缺少稳定请求 ID 的记录可能不在统计中。

## 仓库边界

源码、测试和部署模板可以提交；`.local/`、`reports/`、数据库、个人连接配置、上传令牌、管理员密钥、OpenAI 凭据及 TLS 私钥不得提交。测试中的邮箱和请求数据为合成样例。当前版本的实际部署状态与限制以 [部署说明](README-deploy.md) 为准。
