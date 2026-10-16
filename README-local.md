# 本地 Codex 用量接收服务

本文说明本地启动模式：运行 Python 3.10+ 标准库服务，以 SQLite 保存逐请求数据，网页按邮箱查看 token、每日和模型分布、设备和上报情况。本地模式默认只监听本机回环地址；远端 HTTPS 服务的安装、运行与验收见 [部署说明](README-deploy.md)。

## 启动与登录

在此目录执行 `powershell -NoProfile -File .\run-local.ps1`，打开 `http://127.0.0.1:8765/`。脚本优先采用本机 Codex 捆绑 Python，亦可通过 `-PythonExecutable` 指定解释器。配置保存在 `.local/server-config.json`；使用其中的 `admin_key` 登录本地网页，配置不要分享给上报用户。

管理员在网页登记邮箱，取得该邮箱专属的上报令牌。仅需要邮箱，不收集工号、姓名、部门。上报令牌与 OpenAI 登录凭据完全独立，服务器只保存令牌哈希。邮箱是服务端登记的上报归属，并不证明历史日志确实属于当前 Codex 登录账户。

## 采集和上报

使用 `collector.py collect --period last7 --output reports/summary.json --events-output reports/events.json` 生成本地包。`last-week` 为北京时间上个自然周；也可指定 `--start`、`--end`。

将网页生成的上报令牌存入本地文件，例如 `.local/upload-token.json`，内容为 `{"upload_token":"此处替换为专属令牌"}`。然后执行：

```powershell
python client.py --url http://127.0.0.1:8765 --events reports/events.json --token-file .local/upload-token.json
```

客户端为本机生成并复用 device ID；亦可用 `--device-id` 指定。失败可重传原包，服务器按请求标识去重。客户端不跟随重定向，不向远端明文 HTTP 发送令牌。上述 `python` 需要替换成可运行的解释器，不能使用 Windows Store 占位程序。

## 去重与冲突

请求唯一键来自采集端 `sha256('openai:codex:' + response_id)`，不包含邮箱、设备或报告周期。服务器以团队内唯一请求入库：同机重传、跨机器复制日志和重叠时间范围不重复计算；不同机器的独立请求正常累加。

同一请求标识的 token、时间、模型或上报邮箱不同会产生冲突，进入隔离状态并从有效汇总排除，管理员可以看到冲突提示。批次编号只能追踪重试，不能代替逐请求去重。格式错误的请求包整批拒绝；客户端需查看服务端确认结果，不能把“HTTP 成功”简单视为所有请求均新增。

费用由服务器读取本地 `prices.json` 按逐请求重算，不相信客户端的费用值；未知模型显示未定价及已定价小计。金额是所选价格快照的 Standard API 等值，不是订阅扣费。查询区间按北京时间左闭右开，页面的结束日期不包含当天。

## 接口

- `GET /health`：公开健康检查。
- `POST /api/users`：管理员 Bearer 令牌，JSON 为 `{"email":"user@example.com"}`；201 只返回一次上传令牌，已有邮箱返回 409，不改变旧令牌。
- `GET /api/users`：管理员查询登记邮箱和设备状态，不返回上传令牌。
- `POST /api/events`：邮箱专属上传 Bearer 令牌，JSON 为 `{"device_id":"已有设备标识","package":完整采集包}`。可选 email 必须与服务端令牌所属邮箱一致。
- `GET /api/summary?start=2026-10-09&end=2026-10-17`：管理员查询；可追加 email 筛选，也可使用 `week=周一日期`。
- `GET /api/export.csv`：管理员导出，筛选参数与 summary 相同。

服务端支持最多 16 MiB 请求体和每包 50,000 条事件。鉴权失败为 401，角色/邮箱越权为 403，非法包为 400，过大请求为 413。HTTP 200 的确认中包含新增、重复、冲突和拒绝数量；页面冲突数来自完整汇总，列表只展示最近部分。

界面会标明本期未提交、历史同步、未定价和隔离冲突。首次生成的上传令牌应及时保存；当前版本没有管理员重发/轮换入口，网络中断导致创建响应丢失时也不能通过重复登记恢复令牌。该恢复能力需在后续多人部署前补齐。

## 验证

```powershell
python -m unittest test_collector test_identity test_client test_server test_integration -v
```

自动化测试使用虚构邮箱和临时数据库。真实本机数据与管理员凭据位于 `.local/` 和 `reports/`，已加入忽略规则，不能作为示例上传到仓库。当前版本的账户剩余额度不由 token 推算；页面不能把 API 等值费用当作实际消费。
