# 个人云部署

使用 Ubuntu 24.04、Python 3.12、FFmpeg 和 Chromium。有界面 Chromium 在 Xvfb 内运行，不提供远程桌面。网页使用 `--remote`：文件先完整下载并检查后保存在服务器 Output，用户通过网页预览、下载视频或图集 ZIP。

2026-10-18 状态：正式服务已升级到 **0.8.1**，沿用带访问密码的临时 IP 入口；用户已提交备案，域名 HTTPS 仍待后续接入。sf11 / sf6 节点与原失败任务补保存见[0.8.1 修复记录](validation/2026-10-18-production-0.8.1.md)，真实图集原始 MP3、旧数据保留及浏览器试听边界见[0.8.0 上线记录](validation/2026-10-18-production-0.8.0.md)。批量提交、取消、原选项重试和已知样本新下载见[0.7.0 上线记录](validation/2026-10-18-production-0.7.0.md)，其中单独记录了一条新样本页面读取停滞，未将其计为下载成功。手机界面与视频音频基线见[0.6.0 上线记录](validation/2026-10-17-production-0.6.0.md)，其中区分了 Chromium 试听与 Windows WebKit 对 HE-AACv2 音轨的兼容边界。此前单图、取消及文件管理验收见[0.5.1 上线记录](validation/2026-10-17-production-0.5.1.md)。初次常驻部署记录保留在[原部署验收](validation/2026-10-15-cloud-deploy.md)，域名入口模板及续期钩子尚未启用。

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

### 升级到 0.5.0

本版使用可重复执行的增量 SQLite 迁移，增加重试预算、重复收藏引用和文件删除状态；旧历史及隐藏状态继续保留。新旧服务不能同时访问同一 WebData。升级前停止新任务并等待活动任务结束，停止服务，再通过 SQLite backup 或关闭连接后的数据库副本备份历史，记录旧代码提交；需要保留可恢复的媒体时还须单独备份 Output。

0.5.0 的“删除记录及文件”会实际删除媒体，旧数据库备份不能恢复这些文件。回退前先核对是否执行过删除；若有待处理删除或已删除文件，先保存现有数据库及剩余文件，不要直接恢复旧库造成指向不存在媒体的成功记录。确认代码与数据库 / 文件一致后再重启。尚未完成的删除会保留精确清单；只有输出根目录相同才继续，改变 --output 时须切回原目录处理。

候选验证先使用独立任务库和输出目录，隔离验证不代替生产 systemd 内存限制下的升级验收。升级后应重新核对登录、真实视频 / 图集 / 动态照片、Range 预览、取消、空间统计、保留记录恢复和下载期间拒绝删除。隔离结果见[0.5.0 验证](validation/2026-10-17-retry-storage.md)，生产 0.5.1 的实际覆盖范围见[上线记录](validation/2026-10-17-production-0.5.1.md)。

### 升级到 0.6.0

继续先确认没有主下载或音频处理任务，停止服务，再备份 SQLite、原代码版本和需要保留的 Output。本版为 jobs 增加可选音频与恢复清单字段，旧任务默认不提取音频；M4A 附在原任务下，计入文件管理和空间统计。视频失败不会启动音频，音频失败不影响已完成视频。

实际升级使用原虚拟环境离线重新安装，未更新依赖或更改 systemd / Nginx。回退时先保留当前数据库与新增文件，确认没有升级后新请求或删除操作后，才恢复升级前快照；不能直接用旧库覆盖用户新记录。0.6.0 的备份位于 `WebData/deployments/2026-10-17-0.6.0-3f2e95b/`。

### 升级到 0.7.0

沿用先确认主下载及音频均空闲、停止服务、备份 SQLite 和 Output 的顺序。新增内部字段 `supplement_duplicate_audio`，旧记录默认开启以兼容原单任务行为；批量提交与批量重试关闭，防止后置短链判重自动给旧收藏补音频。没有新增依赖、批次数据库或并行下载进程。

实际备份位于 `WebData/deployments/2026-10-18-0.7.0-714c87b/`。升级及公网验证后已核对全部旧字段、隐藏状态、原索引和原文件哈希；备份及回退脚本不直接覆盖升级后新增的用户记录。保持原认证与端口、systemd 内存限额和 Nginx 配置，服务器以 dev 持有源码和下载文件。

### 升级到 0.8.0

继续确认主下载及音频任务均空闲，停止服务后备份 SQLite、Output、旧代码提交及访问凭据文件哈希。新增 `audio_error_detail` 通过增量迁移接入，旧记录为 `{}`；原视频 M4A 与新图集原始 MP3 共用独立音频队列，保留既有文件索引及历史。

本次固定提交为 `247fcd6`，备份位于 `WebData/deployments/2026-10-18-0.8.0-247fcd6/`。原 23 条任务、31 个索引、42 个文件哈希及密码在启动后和真实下载后均核对一致。使用原虚拟环境离线安装，没有新增依赖；回退脚本只在确认没有新用户记录或文件变化后恢复旧库，避免覆盖升级后的内容。完整范围见[上线记录](validation/2026-10-18-production-0.8.0.md)。

### 升级到 0.8.1

本版只补充两个实测音频节点并更新版本号，没有新增依赖或数据库结构变更。仍先确认主下载 / 音频均空闲，停止服务并备份数据库、Output、源码提交及密码哈希，再离线安装和核对原历史。固定运行提交为 `eb0fe42`，备份在 `WebData/deployments/2026-10-18-0.8.1-eb0fe42/`；正式服务只为用户原失败任务补保存配乐，未重新抓取图片。最终核对允许该目标的音频字段按附件成功变化，其他旧字段、旧索引及文件哈希全部保留。过程及首次补保存超时后的实际重试结果见[修复记录](validation/2026-10-18-production-0.8.1.md)。

## 云端登录

界面、会话保护、完整回归及真实公网验收见[登录体验验证记录](validation/2026-10-15-cloud-login.md)。

0.4.4 修复 iPhone Safari 将同源 POST 的 Origin 发为 null 的问题；已通过真实 WebKit 和公网验证，见[Safari 修复记录](validation/2026-10-16-safari-login.md)。

0.4.5 补充单页动态照片的图片和动态片段配对下载，以及媒体提取失败诊断，见[单页图文验证](validation/2026-10-16-single-live-note.md)。

0.4.3 使用光匣自己的登录页，云端只需输入访问密码。凭据文件 `WebData/access.json` 至少包含一个非空的 `password` 字符串，由 dev 持有、权限 0600，不提交 Git。服务用 `--access-file /home/dev/develop/lumabox/WebData/access.json` 启用保护；读取失败或格式错误时拒绝启动，文件路径不传给下载子进程。

会话使用随机令牌和 HttpOnly / SameSite=Strict Cookie，令牌本身不包含密码。可勾选记住登录 7 天，不勾选时使用浏览器会话 Cookie且服务端最多保留 8 小时；服务重启或点击退出会要求重新登录。连续 5 次密码错误后短暂限流。收藏列表、下载和预览都要求有效会话。默认本地运行不指定 access-file，继续免登录。

从旧 Nginx Basic 认证迁移时，先保留旧认证、启用应用保护，确认回环匿名 API 已返回 401、正确密码可建立会话后，才移除 Nginx Basic 认证，避免迁移期间暴露收藏。沿用旧访问密码时可只从原私有凭据文件复制 password 字段，不需重新设置密码。

## HTTPS 入口

程序始终只监听 `127.0.0.1:8765`。公网由 [Nginx 模板](../deploy/lumabox-site.conf) 提供 HTTPS，应用通过 access-file 保护云端入口。[服务模板](../deploy/lumabox.service) 同时启用 `--secure-cookie`，确保登录 Cookie 只通过 HTTPS 发送。本地版仍直接使用，抖音浏览器仍为匿名会话。

模板要包含在实际运行的 Nginx `http` 块中。当前服务器使用 `/etc/nginx/ghost-chat.conf`，不是发行版默认配置。变更前备份该文件，不覆盖其他站点，使用以下命令验证后才重载：

```bash
nginx -t -c /etc/nginx/ghost-chat.conf
systemctl reload nginx
```

证书需包含实际完整子域名，现有其他站点的证书不能直接复用。HTTP-01 使用 `/var/lib/ghost-chat-acme`；访问密码只放在私有 access-file 中，不写入模板或提交记录。

Nginx 先校验来自浏览器的公网 Origin，再将 Host / Origin 翻译为本机服务接受的值；原有 CSRF、跨站请求和请求体上限继续生效。应用对未经认证的页面请求跳转登录，对收藏及文件 API 返回 401；不开放 8765 公网端口。

证书申请成功后安装 [续期钩子](../deploy/lumabox-renew-hook.sh) 到 `/etc/letsencrypt/renewal-hooks/deploy/` 并赋予可执行权限，确认 certbot 定时器和续期演练。钩子只处理本站证书，使用实际配置测试成功才重载 Nginx。

## 临时 IP 体验入口

用户申请备案后要求先通过服务器 IP 体验，使用 [IP 入口配置](../deploy/lumabox-ip-preview.conf)，仅匹配 IP Host，独立于原有 Ghost Chat 站点。配置包含在当前生效的 `/etc/nginx/ghost-chat.conf` 中；变更前已在 WebData/backups 备份，配置测试成功才重载。

当前应用使用 `/etc/systemd/system/lumabox.service.d/private-preview.conf` 覆盖启动命令，将回环端口临时设为 18765；数据目录、输出目录和 dev 用户保持不变。Nginx 对 IP 入口先校验公网 Origin，再翻译到该回环端口。公网仅使用已有 80 端口，没有开放应用端口。

原 Nginx 认证使用独立随机密码，旧凭据位于 `WebData/ip-preview-access.json`。升级后将同一密码接入 `WebData/access.json`，网页不再要求输入用户名；停用 Basic 认证后移除旧的 htpasswd 文件。凭据没有提交 Git。此入口为 HTTP，不具备 HTTPS 的传输加密，临时覆盖启动命令不启用 secure-cookie；临时密码不应复用到其他服务。

切换正式域名时，先确认无活动任务并备份数据库；移除预览 drop-in 后 daemon-reload / restart，确认应用回到 127.0.0.1:8765 且开启 access-file / secure-cookie；再启用域名 HTTPS 配置，移除 IP 入口 include，配置验证成功后重载。完成域名实测后清理旧 IP 入口凭据副本，保留应用实际使用的 access.json。

## 运维检查

```bash
systemctl status lumabox --no-pager
journalctl -u lumabox -n 60 --no-pager
systemctl show lumabox -p MemoryCurrent -p MemoryPeak -p NRestarts
ss -ltnp 'sport = :8765'
```

部署验收要覆盖 systemd 约束下的真实下载、整组 ZIP、视频范围预览、取消及队列恢复、认证与跨站写入拒绝、原有站点、重启后历史保留。命令行成功不能代替这一步，也不能代替公网证书及域名可访问验证。
