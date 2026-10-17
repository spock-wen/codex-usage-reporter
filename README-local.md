# 本地 Codex 用量接收服务

本文说明本地启动模式：运行 Python 3.10+ 标准库服务，以 SQLite 保存逐请求数据，网页按邮箱查看 token、每日和模型分布、设备和上报情况。本地模式默认只监听本机回环地址；远端 HTTPS 服务的安装、运行与验收见 [部署说明](README-deploy.md)。

## 启动与登录

在此目录执行 `powershell -NoProfile -File .\run-local.ps1`，打开 `http://127.0.0.1:8765/`。脚本优先采用本机 Codex 捆绑 Python，亦可通过 `-PythonExecutable` 指定解释器。配置保存在 `.local/server-config.json`；使用其中的 `admin_key` 登录本地网页，配置不要分享给上报用户。

新版主流程为自动接入：客户端读取本机 Codex 登录邮箱，注册本设备上传凭据，无需管理员预登记或邮件验证码。界面明确标记“内部自报 · 邮箱未验证”，这不是可信 SSO 身份，也不能证明历史日志归属。看板查看和团队 API 仍需管理员密钥。上报不收集工号、姓名或部门，不使用 OpenAI 登录令牌。

## 采集和上报

本机服务启动后，客户端明确指定本地地址接入，再上报：

```powershell
python enrollment.py --url http://127.0.0.1:8765
python report.py run --period last-week --output-dir reports/local-weekly
```

需要纯本地统计用 `python report.py run --period last-week --no-upload --output-dir reports/local`，不联网、不查询登录身份。不指定周期时也默认 last-week，为北京时间上个自然周；只有明确需要最近七个滚动日时才用 --period last7；历史补报可用 collector 的 --start/--end。远端同事安装见 [指南](docs/colleague-guide.md)。

首次接入后明确要求同时上报上周与本周时，先冻结一个带时区的 ISO 截止时刻，分别运行 `report.py run --period last-week --now <同一截止时刻>` 和 `report.py run --period this-week --now <同一截止时刻>`；本周为北京时间本周一 00:00 至该截止时刻，两次共用 --now，执行跨周也保持区间连续、不重叠。两个周期分别保存报告和核对回执。每周一 10:00 的定时仍只上报 last-week，并使用每次实际运行时刻。周一精确 00:00 没有正长度本周区间，返回 current_week_not_started，不上传；稍后再补报本周。

以下仅为旧版手动接入兼容：管理员在网页“手动兼容接入”登记邮箱并取得个人令牌，再用 `collector.py collect --period last-week --output reports/summary.json --events-output reports/events.json` 生成本地包。

将网页生成的上报令牌存入本地文件，例如 `.local/upload-token.json`，内容为 `{"upload_token":"此处替换为专属令牌"}`。然后执行：

```powershell
python client.py --url http://127.0.0.1:8765 --events reports/events.json --token-file .local/upload-token.json
```

客户端为本机生成并复用 device ID；亦可用 `--device-id` 指定。失败可重传原包，服务器按请求标识去重。客户端不跟随重定向，不向远端明文 HTTP 发送令牌。上述 `python` 需要替换成可运行的解释器，不能使用 Windows Store 占位程序。

用户明确授权每周自动上报时，用 Codex 原生自动化每周一北京时间 10:00 运行 `report.py run --period last-week --max-upload-delay-seconds 600`。同用途任务复用不重复创建；首次立即上报或普通手动上报不加错峰参数。该参数为 0–600 的整数，默认 0；程序先保存固定报告和事件包，再在真正上传前随机延迟，不更换批次。`--no-upload` 不延迟。上传默认每次 60 秒，重传包可用 `client.py --timeout 120` 放宽，最多三次、总调用限时 370 秒；注册仍为每次 15 秒、总 55 秒。不使用此参数或更新程序本身不会自动创建计划任务。

## 去重与冲突

请求唯一键来自采集端 `sha256('openai:codex:' + response_id)`，不包含邮箱、设备或报告周期。服务器以团队内唯一请求入库：同机重传、跨机器复制日志和重叠时间范围不重复计算；不同机器的独立请求正常累加。

同一请求标识的 token、时间、模型或上报邮箱不同会产生冲突，进入隔离状态并从有效汇总排除，管理员可以看到冲突提示。批次编号只能追踪重试，不能代替逐请求去重。格式错误的请求包整批拒绝；客户端需查看服务端确认结果，不能把“HTTP 成功”简单视为所有请求均新增。

费用由服务器读取本地 `prices.json` 按逐请求重算，不相信客户端的费用值；未知模型显示未定价及已定价小计。金额是所选价格快照的 Standard API 等值，不是订阅扣费。查询区间按北京时间左闭右开，页面的结束日期不包含当天。

## 接口

- `GET /health`：公开健康检查。
- `POST /api/enroll`：匿名内部自报注册，严格 JSON 为 email/device_id/registration_key；同一设备注册记录可重试，响应身份级别为 self_reported。公开请求体上限 8 KiB；不验证邮箱所有权。
- `POST /api/users`：旧手动接入兼容，管理员 Bearer 令牌，JSON 为 `{"email":"user@example.com"}`；201 只返回一次上传令牌，已有邮箱返回 409，不改变旧令牌。
- `GET /api/users`：管理员查询登记邮箱和设备状态，不返回上传令牌。
- `POST /api/events`：邮箱专属上传 Bearer 令牌，JSON 为 `{"device_id":"已有设备标识","package":完整采集包}`。可选 email 必须与服务端令牌所属邮箱一致。
- `GET /api/summary?start=2026-10-09&end=2026-10-17`：管理员查询；可追加 email 筛选，也可使用 `week=周一日期`。
- `GET /api/export.csv`：管理员导出，筛选参数与 summary 相同。

服务端支持最多 16 MiB 请求体和每包 50,000 条事件。鉴权失败为 401，角色/邮箱越权为 403，非法包为 400，过大请求为 413。HTTP 200 的确认中包含新增、重复、冲突和拒绝数量；页面冲突数来自完整汇总，列表只展示最近部分。

界面会标明本期未提交、历史同步、未定价和隔离冲突。自动注册使用本机持久注册记录重试，不轮换其他机器或旧版凭据；禁用邮箱不能通过再次注册恢复。旧手动令牌仍只显示一次，没有自助重发入口，重复手动登记不会恢复令牌。

## 验证

```powershell
python -m unittest test_collector test_identity test_client test_server test_integration -v
```

自动化测试使用虚构邮箱和临时数据库。真实本机数据与管理员凭据位于 `.local/` 和 `reports/`，已加入忽略规则，不能作为示例上传到仓库。当前版本的账户剩余额度不由 token 推算；页面不能把 API 等值费用当作实际消费。
