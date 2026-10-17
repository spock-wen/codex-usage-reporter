# codex.wenspock.site 部署与旧域名兼容

目标主机为 `223.109.200.65`，主域名为 `codex.wenspock.site`；原 `usage.wenspock.site` 保留独立 HTTPS 虚拟主机，直接反代同一后端。两域名共用原数据库、管理员密钥和设备凭据，无需重新注册。旧 HTTPS API 不跳转到新域名，因为采集客户端拒绝跟随重定向。

主入口为 `https://codex.wenspock.site`。两个域名的 DNS A 均为 DNS-only，分别签发并续期证书，后端由原 `codex-usage.service` 开机启动。下面步骤包含新主域名迁移，新的 DNS、TLS 与实际上传结果应以迁移后的验收记录为准；旧域名已有的验收报告不等于新域名已验收。Debian 12 现有 Python 3.11、Nginx 和 Certbot 可直接使用，无 Python 第三方依赖。

2026-10-16 已完成双域名上线验收：新域名证书有效至 2027-01-14，两个域名的普通 HTTPS 访问、注册重试和重复上报均通过，未使用 IP 覆盖或关闭证书验证。两端同包重传新增为零、统计一致；原数据库、凭据及其他站点配置保持不变。新旧证书的 Certbot 模拟续期分别通过，两种 lineage 的 reload hook 均通过 `nginx -t` 后成功重载。私人验收记录在 `reports/codex-domain-live-validation.json`、`reports/codex-domain-preservation.json`，不随源码公开。

成员使用 `python scripts/manage_skill.py install --enroll` 自动读取本机 Codex 登录邮箱接入，无需管理员手签令牌或邮箱验证码；也可单独执行安装后的 scripts/enrollment.py。首次上报缺配置时同样自动接入。私人配置、设备标识、注册记录和上传凭据保存在 CODEX_HOME/codex-usage，未设置时为 ~/.codex/codex-usage；管理员密钥不要分发给成员。每周一显式运行 scripts/report.py run --period last-week，上报失败重传已保存事件包。

接入身份为“内部自报 · 邮箱未验证”，不是可信 SSO。匿名 POST /api/enroll 只用于取得本设备上传凭据，团队统计/CSV/成员查询仍需管理员鉴权。独立凭据表兼容旧数据库和上传令牌，不轮换其他设备。注册使用持久管理员密钥派生独立用途的凭据，只存注册秘密和令牌的 hash；更新部署保留既有 admin_key，更换该密钥会影响同一注册记录的凭据重建。

迁移原因：本机 Clash 订阅曾有 `DOMAIN-KEYWORD,usage,REJECT` 规则，导致包含 usage 的域名被误拦截。主域名改为 `codex.wenspock.site`，同事新安装默认使用新入口。既有 remote.json 不会被更新安装自动改写，继续使用旧地址即可，无需迁移。已有配置直接执行 enrollment.py --url 新入口会被目的地址一致性检查拒绝。若用户明确要求切换，并已确认两个地址属于同一服务，由维护者先备份私人 remote.json，再仅将其中 url 改为 https://codex.wenspock.site，保留原凭据和 device_id；也可使用独立 --config 路径重新自动接入，不覆盖旧配置。

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
certbot certonly --webroot -w /var/www/letsencrypt -d codex.wenspock.site --email YOUR_CERT_CONTACT_EMAIL --agree-tos --non-interactive

# 证书签发成功后启用独立 HTTPS 虚拟主机。
sh /root/codex-usage-stage/deploy/install-release.sh /root/codex-usage-stage tls
nginx -t
curl --fail https://codex.wenspock.site/health
curl --fail https://usage.wenspock.site/health
systemctl is-active certbot.timer
```

bootstrap 只安装新主域名的 HTTP challenge 虚拟主机，不触碰正在运行的旧域名。新域名其余 HTTP 请求跳转自身 HTTPS；证书未签发前，新域名 HTTPS 暂不可用，旧域名继续服务。正式主域名虚拟主机为 `/etc/nginx/sites-available/codex.wenspock.site`，旧域名为 `/etc/nginx/sites-available/usage.wenspock.site`，各自保留同名 sites-enabled 链接。主证书路径为 `/etc/letsencrypt/live/codex.wenspock.site/`，旧证书路径为 `/etc/letsencrypt/live/usage.wenspock.site/`。

TLS 模式要求上述两个证书的 fullchain.pem 和 privkey.pem 均已存在，然后安装 `deploy/nginx.conf` 与 `deploy/nginx-legacy.conf`；旧 HTTPS 根路径和 API 保持直接代理，不重定向。首次安装若没有旧域名证书，需先按同样的 webroot 流程为旧域名准备 challenge 配置并签发证书，再运行 tls；不要用其他证书冒充，也不要覆盖不属于此服务的虚拟主机。安装器在 none 模式不改 Nginx，在 bootstrap 模式只改新站，在 tls 模式仅管理这两个站，拒绝覆盖无法识别的配置或非预期启用链接。

续期沿用现有 Certbot timer，不更改其他证书的配置或续期钩子。安装器在 TLS 模式只新增独立 deploy hook `codex-usage-reload-nginx`，仅在这两个证书更新后先 `nginx -t` 成功再 reload。可安排维护窗口分别验证，两个证书均应保持续期：

```sh
certbot renew --cert-name codex.wenspock.site --dry-run
certbot renew --cert-name usage.wenspock.site --dry-run
```

Nginx 请求体上限 17 MiB，后端实际限制 16 MiB/每批 50000 事件；代理超时 60 秒。前后端均 no-store，不开放跨域。此虚拟主机禁用 access log 与请求级 error log，避免邮箱查询和认证相关元数据落入请求日志；服务启动/状态可从 systemd journal 查看，不在命令行参数或 URL 传密钥。此版本未额外引入全局 Nginx 限流 zone。

## 更新与回退

更新仍先暂存审阅后的固定清单，运行安装器 `none`（只更新后端）或 `tls`（同时更新本站配置）。安装器在修改前，将新旧两个域名的现有虚拟主机配置、systemd unit、独立钩子及旧代码备份到 `/var/backups/codex-usage/<版本>`；数据库使用 SQLite online backup，不复制一个正在写入的裸数据库文件。旧 release 原地保留，无递归删除。任何现有凭据均保留；失败时先检查备份路径，不以重新生成管理员密钥“修复”。

代码回退使用备份中的 `previous-release.txt`，人工核实其位于 `/opt/codex-usage/releases/` 后，将 `current` 原子切回旧版本再 `systemctl restart codex-usage`。如果需要回退本站 Nginx/unit，先从对应备份恢复原文件，运行 `systemd-analyze verify` / `nginx -t`，成功后再 daemon-reload、重启或 reload。数据库回滚可能丢失更新期间已接收的事件，需单独决策；普通代码回退不要覆盖现有数据库。

## 默认统计区间

服务端未指定 week/start/end 时与看板首次打开均默认上周自然周：北京时间上周一 00:00 至本周一 00:00（不含）。显式日期或 week 参数保持原行为。看板“近 7 个自然日”包含今天、按北京时间午夜切分，与采集程序显式 last7 的滚动 7×24 小时不同。

## 部署验收

操作者应在主机上实际完成 `systemd-analyze verify`、`nginx -t`、服务 active/enabled、loopback 监听以及外部 HTTPS 证书验证。分别验证新旧域名 `/health` 为 200、HTTPS 无重定向且证书匹配，匿名访问 `/api/summary` 返回 401、上传 token 不能读取团队统计；管理员通过本地受控方式取得私有配置中的 key，不粘贴到共享日志。

在 HTTPS 域名通过本机正常登录邮箱自动接入，验证相同注册记录重试取得相同凭据、禁用邮箱不能重新启用、旧令牌仍可用。重新采集后上报同一包两次：第二次 ACK 应 new=0、重复条数增加而累计 token 不变；同一事件包分别经新旧域名上报，也必须全局去重，不能因入口域名改变而重复计算。第二设备各自注册，独立请求应累加，克隆同一请求不得累加；跨邮箱或相同请求内容矛盾须隔离。服务不接收 OAuth 凭据；邮箱只是未验证的自报归属，不能据当前账户推断历史日志的真实归属。

部署文件准备、配置检查与本地测试不等同于公网部署验收；只有操作者取得上述远程运行证据后才可声称上线。
