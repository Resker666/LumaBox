# 个人云部署

使用 Ubuntu 24.04、Python 3.12、FFmpeg 和 Chromium。有界面 Chromium 在 Xvfb 内运行，不提供远程桌面。网页使用 `--remote`：文件先完整下载并检查后保存在服务器 Output，用户通过网页预览、下载视频或图集 ZIP。

## 目录与运行方式

- `/home/dev/develop/lumabox`：代码和 `.venv`，以 dev 运行。
- `WebData/`：任务数据库、浏览器缓存及运行配置。
- `Output/`：正式网页下载文件；`Downloaded/cloud-p0/` 保留命令行验证报告和文件。
- `/etc/systemd/system/lumabox.service`：开机启动，失败自动重启，浏览器及下载进程属于同一个服务控制组。

以上运行数据及凭据均从 Git 排除。当前模板设置 1100 MiB 内存上限、一次处理一个任务。模板针对当前机器路径与域名，换服务器时需逐项修改并重新验证。

安装系统依赖 `python3-venv ffmpeg xvfb xauth` 和 Playwright 对应的浏览器依赖。建立虚拟环境并安装 `.[web]`，再以 dev 安装浏览器：

```bash
export PLAYWRIGHT_BROWSERS_PATH=/home/dev/develop/lumabox/.venv/browsers
.venv/bin/python -m playwright install chromium --no-shell
mkdir -p WebData/cache WebData/config Output
```

将 [服务模板](../deploy/lumabox.service) 安装到 systemd，执行 `systemd-analyze verify` 后再 `systemctl enable --now lumabox`。更新时先确认没有活动任务，记录旧提交并备份 SQLite，再切换代码、更新虚拟环境和重启服务；失败时切回旧提交。不要用新空数据库覆盖原历史。

## HTTPS 入口

程序始终只监听 `127.0.0.1:8765`。公网由 [Nginx 模板](../deploy/lumabox-site.conf) 提供 HTTPS 与独立的 HTTP Basic 认证；这是云端入口密码，本地版仍直接使用，抖音浏览器仍为匿名会话。

模板要包含在实际运行的 Nginx `http` 块中。当前服务器使用 `/etc/nginx/ghost-chat.conf`，不是发行版默认配置。变更前备份该文件，不覆盖其他站点，使用以下命令验证后才重载：

```bash
nginx -t -c /etc/nginx/ghost-chat.conf
systemctl reload nginx
```

证书需包含实际完整子域名，现有其他站点的证书不能直接复用。HTTP-01 使用 `/var/lib/ghost-chat-acme`；创建仅含账号密码哈希的 `/etc/nginx/lumabox.htpasswd`，root 所有、www-data 可读，权限 0640。密码不写入模板或提交记录。

Nginx 先校验来自浏览器的公网 Origin，再将 Host / Origin 翻译为本机服务接受的值；原有 CSRF、跨站请求和请求体上限继续生效。不把未经认证的外部请求转发到应用，不开放 8765 公网端口。

证书申请成功后安装 [续期钩子](../deploy/lumabox-renew-hook.sh) 到 `/etc/letsencrypt/renewal-hooks/deploy/` 并赋予可执行权限，确认 certbot 定时器和续期演练。钩子只处理本站证书，使用实际配置测试成功才重载 Nginx。

## 运维检查

```bash
systemctl status lumabox --no-pager
journalctl -u lumabox -n 60 --no-pager
systemctl show lumabox -p MemoryCurrent -p MemoryPeak -p NRestarts
ss -ltnp 'sport = :8765'
```

部署验收要覆盖 systemd 约束下的真实下载、整组 ZIP、视频范围预览、取消及队列恢复、认证与跨站写入拒绝、原有站点、重启后历史保留。命令行成功不能代替这一步，也不能代替公网证书及域名可访问验证。
