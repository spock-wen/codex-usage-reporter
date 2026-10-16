# usage.wenspock.site 部署

目标主机为 `223.109.200.65`，域名 `usage.wenspock.site`。已于 2026-10-16 完成实际部署，独立验收记录见 `reports/deployment-audit.json`，实际采集和幂等验证见 `reports/production-deployment-validation.json`。Debian 12 现有 Python 3.11、Nginx 和 Certbot 可直接使用，无 Python 第三方依赖。

公网地址为 `https://usage.wenspock.site`，DNS A 为 DNS-only，后端由独立 systemd unit 开机启动。证书有效至 2027-01-14，使用现有 Certbot timer 续期。已验证服务重启后的数据持久化，没有重启整台共享服务器。

本机私人配置为 `~/.codex/codex-usage/remote.json`，令牌单独存于同目录 `upload-token.json`；管理员密钥副本位于项目 `.local/remote-admin.json`，不要分发给成员。安装的 skill 用 `scripts/report.py run --period last7 --output-dir <报告目录>` 完成一次扫描和有界上报，上周自然周使用 `last-week`。重传已保存事件包用 `scripts/client.py --events <events.json>`。

2026-10-16 补充验收：此前默认域名连接关闭的根因是本机 Clash 的 `DOMAIN-KEYWORD,usage,REJECT` 广告规则。已在当前订阅的持久规则中仅 prepend `DOMAIN,usage.wenspock.site,DIRECT`，保留其他拦截，重载后普通域名 GET 200、内置浏览器打开并登录成功。私人 remote.json 已移除 connect_ip，原生 skill CLI 使用普通域名重传仍 new=0、duplicates=15728。证据见 `reports/normal-domain-access-validation.json` 及 `reports/remote-dashboard-access-verified.png`。

## 文件与隔离

- 最小源码清单见 [deploy/source-manifest.txt](deploy/source-manifest.txt)：`server.py`、`collector.py`、`prices.json`、`web/index.html`、`web/app.js`、`web/style.css`。服务端直接从 collector 导入 token 定义、时间解析和计价函数；不需要 identity、client、测试、真实报告或本机认证文件。
- 代码存于 `/opt/codex-usage/releases/<版本>`，`current` 指向当前版本。代码 root 所有、服务用户只读。
- 单独系统用户/组 `codex-usage`，shell 为 `/usr/sbin/nologin`；SQLite 数据在 `/var/lib/codex-usage/usage.sqlite3`，仅服务用户可写。
- 配置 `/etc/codex-usage/server.json` 为 `root:codex-usage 0640`，内容仅需 `{"admin_key":"至少24字符的随机密钥"}`。安装器只在不存在时生成，不打印、不轮换既有密钥，不备份到源码目录。
- systemd 开机自启，失败重启，仅监听 `127.0.0.1:18765`；Nginx 在 443 反代根路径，不使用子路径重写。后台数据库和凭据均不在网站静态白名单内。

## 安装与 HTTPS

先核实 DNS A 指向目标主机；如配置 AAAA，必须确保 IPv6 同样可达。检查端口 18765、既有虚拟主机和 Certbot timer，不覆盖其他站点。将上述六个源码文件及 `deploy/` 放入独立的服务器暂存目录，如 `/root/codex-usage-stage`；暂存目录不得是 `/opt/codex-usage` 内部。

```sh
# 在目标 Linux 主机以 root 执行；替换为实际暂存路径。
sh /root/codex-usage-stage/deploy/install-release.sh /root/codex-usage-stage bootstrap
curl --fail http://127.0.0.1:18765/health
systemctl is-enabled codex-usage.service
systemctl is-active codex-usage.service

# 用自己的证书联系邮箱。此命令仅申请独立域名证书，不改写 nginx 配置。
certbot certonly --webroot -w /var/www/letsencrypt -d usage.wenspock.site --email YOUR_CERT_CONTACT_EMAIL --agree-tos --non-interactive

# 证书签发成功后启用独立 HTTPS 虚拟主机。
sh /root/codex-usage-stage/deploy/install-release.sh /root/codex-usage-stage tls
nginx -t
curl --fail https://usage.wenspock.site/health
systemctl is-active certbot.timer
```

首次 bootstrap 阶段只有 ACME HTTP challenge 可用，其余 HTTP 请求跳转 HTTPS；证书未签发前 HTTPS 暂不可用。正式虚拟主机为 `/etc/nginx/sites-available/usage.wenspock.site`，启用链接为同名 `sites-enabled` 文件。证书固定使用 `/etc/letsencrypt/live/usage.wenspock.site/` 下的 `fullchain.pem` 与 `privkey.pem`。

续期沿用现有 Certbot timer，不更改其他证书的配置或续期钩子。安装器在 TLS 模式只新增独立 deploy hook `codex-usage-reload-nginx`，先 `nginx -t` 成功再 reload。可安排维护窗口运行 `certbot renew --cert-name usage.wenspock.site --dry-run`；不要为了本站重写全局续期任务。

Nginx 请求体上限 17 MiB，后端实际限制 16 MiB/每批 50000 事件；代理超时 60 秒。前后端均 no-store，不开放跨域。此虚拟主机禁用 access log 与请求级 error log，避免邮箱查询和认证相关元数据落入请求日志；服务启动/状态可从 systemd journal 查看，不在命令行参数或 URL 传密钥。此版本未额外引入全局 Nginx 限流 zone。

## 更新与回退

更新仍先暂存审阅后的固定清单，运行安装器 `none`（只更新后端）或 `tls`（同时更新本站配置）。安装器在修改前，将本站现有配置、systemd unit、独立钩子及旧代码备份到 `/var/backups/codex-usage/<版本>`；数据库使用 SQLite online backup，不复制一个正在写入的裸数据库文件。旧 release 原地保留，无递归删除。任何现有凭据均保留；失败时先检查备份路径，不以重新生成管理员密钥“修复”。

代码回退使用备份中的 `previous-release.txt`，人工核实其位于 `/opt/codex-usage/releases/` 后，将 `current` 原子切回旧版本再 `systemctl restart codex-usage`。如果需要回退本站 Nginx/unit，先从对应备份恢复原文件，运行 `systemd-analyze verify` / `nginx -t`，成功后再 daemon-reload、重启或 reload。数据库回滚可能丢失更新期间已接收的事件，需单独决策；普通代码回退不要覆盖现有数据库。

## 部署验收

操作者应在主机上实际完成 `systemd-analyze verify`、`nginx -t`、服务 active/enabled、loopback 监听以及外部 HTTPS 证书验证。验证匿名访问 `/api/summary` 返回 401、上传 token 不能读取团队统计；管理员通过本地受控方式取得私有配置中的 key，不粘贴到共享日志。

在 HTTPS 域名创建真实邮箱的上传 token，并让本机 skill 重新采集后上报同一包两次：第二次 ACK 应 `new=0`、重复条数增加而累计 token 不变。第二设备的独立请求应累加，克隆同一请求不得累加；跨邮箱或相同请求内容矛盾须隔离。公网服务器不接收 OAuth 凭据；邮箱归属是管理员绑定的上传归属，不能据当前登录账户推断历史日志的真实归属。

部署文件准备、配置检查与本地测试不等同于公网部署验收；只有操作者取得上述远程运行证据后才可声称上线。
