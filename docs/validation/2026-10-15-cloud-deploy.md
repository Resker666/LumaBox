# 云部署验收

日期：2026-10-15。运行候选提交 f0f4850a9e41383fcee1cca7c094189cb6018ac1，开发版本 0.4.2。状态：后台已经部署并验证；用户申请备案并等待审核，域名 HTTPS 尚未上线；随后按用户要求完成临时 IP 入口验收。

## 环境与部署范围

Ubuntu 24.04.2，Python 3.12.3，2 核、1613 MiB 内存、无交换分区。代码和环境在 `/home/dev/develop/lumabox`，systemd 以 dev 运行；应用固定监听 `127.0.0.1:8765`，未开放新的公网端口。下载使用完整 Chromium 145 / Playwright 1.58.0 + Xvfb，未验证无头模式。

安装了 FFmpeg、Xvfb 和 Chromium 所需系统库。PyPI 默认源较慢，安装使用阿里云 HTTPS 镜像，仅在本次命令指定，没有修改全局 pip 配置。完整 Chromium 和 Playwright FFmpeg 已安装；未使用的可选 headless shell 下载中止。虚拟环境 `pip check` 通过。

## 自动回归与审查

Windows 完整回归：**354 项通过、1 项跳过、1 条已有警告，233.26 秒**。跳过项需要 Windows 符号链接权限，警告来自已有 Starlette / HTTPX 弃用提示。报告位于本地忽略目录 `.superpowers/cloud-deploy/windows-full.xml`。

两项新增云端呈现测试先观察失败，再实现 `--remote` 及呈现调整。真实 Chromium 在 HTTP 测试服务上验证手机尺寸布局、隐藏打开本机文件夹、视频和图集 ZIP 下载的字节一致性。测试媒体由本地生成；独立审查覆盖回环监听、Nginx 外部 Origin 校验后翻译、入口认证、可写目录边界和续期钩子，没有遗留审查问题。

本机原有 0.4.1 网页服务未停止、未升级；云部署使用独立的远端数据库与输出目录。

## 真实云网络下载

使用用户已授权的两个作品，不导入账号或 Cookie，不修改 DNS 地址检查。

- 视频 7686430330920075249：命令行真实下载成功，1920×1080、124.6 秒、3,316,797 字节，完整解码通过。SHA-256 为 `226a444d446103f423eb09cb38333a7ecbf7e9cdebfe773a99d5b6e25b54edb2`，与先前 Windows 样本相同。命令行运行 ID 为 415b391b4cb345fc8e6c3a8f3282ac5b。
- 图集 7695689642767235187：首次命令行运行在 media_verification 阶段返回 DOWNLOAD_FAILED，没有保留足以判定根因的异常信息。未修改下载内核，重试成功：12/12 张 WebP、3,211,856 字节，逐张解码及打包成功。成功运行 ID 为 659133514fa94226961c099fa1d2cdd7；初次失败报告保留，不把重试成功描述为根因已修复。

## 精确 systemd 环境验收

安装仓库内服务模板并通过 `systemd-analyze verify`，启用开机启动。验证器从回环 HTTP API 提交真实作品，由常驻服务的下载进程处理，验证器本身不替代工作进程。

- 视频任务 15107babf0f54c93870abfaf7ee6ebec 成功；通过文件 API 获取完整 MP4，大小及上述哈希一致，报告 full_decode 为 true。
- 视频预览请求 `Range: bytes=0-1023` 返回 206，Content-Range 和实际 1024 字节均正确。
- 运行中取消任务 e3f7dc088a8040fe8e87a410bef17c1e，观察 running → cancelling → cancelled，任务未完成目录已清理。
- 取消后队列中的图集任务 6517915b678a4215a14f2deb6abc10eb 继续并成功，12/12 张完整解码。通过文件 API 下载整组 ZIP，与服务器 ZIP 字节一致，包内每张图片哈希与成功报告一致。
- 错误 Origin 和缺失 CSRF 的写入请求均返回 403。
- 验证期间服务没有自动重启，cgroup oom / oom_kill 均为 0；服务内存峰值约 1100.2 MiB，触及配置上限并发生内存回收。小内存机器处理较慢，尚未验证云端长视频或更高分辨率作品的表现。
- 无活动任务时用 SQLite backup API 备份历史，再重启服务；3 条任务和 14 条公开文件记录保持一致，所有文件大小及 SHA-256 再次核对通过。重启后保持 active / enabled、NRestarts=0。

实际运行验证数据位于服务器 `WebData/cloud-web-validation.json`，副本位于本地忽略目录 `.superpowers/cloud-deploy/`；成功文件位于服务器 Output。数据库备份位于 `WebData/backups/cloud-before-restart-20261015T143921Z.sqlite3`。运行数据、媒体和备份不进入 Git。

## 公网阻断与后续

子域名 lumabox.resker666.cloud 正确解析到目标服务器。服务器回环访问 HTTP-01 测试文件返回 Nginx 200；本地公网访问则返回 `Server: Beaver`、403 和阿里云备案拦截页。证书客户端早期请求有间歇性超时，连接恢复后的真实 HTTP-01 验证由证书机构明确返回 unauthorized，原因为校验文件收到 403。

用户已说明域名在阿里云备案，当前无法仅凭拦截页判定具体备案异常。需根据阿里云备案控制台的实际网站备案及接入状态处理；[阿里云官方排查说明](https://help.aliyun.com/zh/icp-filing/basic-icp-service/web-site-for-the-record-to-block-1)列出对应检查项。

原有 chat.resker666.cloud 当前公网 HTTP 同样返回 Beaver 403，公网 HTTPS 连接重置。服务器回环 HTTP / HTTPS 均返回 404，与本轮部署开始时的基线一致；当前原站点配置明确返回 404。首次域名部署阶段未修改原有两份 Nginx 配置，也未重载 Nginx。其配置修改时间分别为当天 15:00 和 17:45（北京时间），早于本轮常驻服务部署。之后 IP 体验阶段仅为主配置新增独立 include，原有 Ghost Chat 站点配置保持不变。

公网 HTTPS 证书尚未签发，Nginx 光匣入口模板、访问认证和续期钩子未启用，也未验收公网下载、认证拒绝或续期演练。解除公网阻断后，应继续完成这些步骤；后台下载成功不等于公网部署全部完成。

## 后续：临时 IP 体验

用户确认已申请备案、尚待审核，明确要求先用服务器 IP 体验。公网 IP 起初可到达 Nginx，返回 404；域名仍被前置网关拦截，二者实际响应不同。

短暂建立 SSH 私有预览后，用户再次明确要求 IP 入口；随后安装独立 `lumabox-ip-preview.conf`，使用已有公网 80 端口和独立随机 Basic 认证，不覆盖 Ghost Chat server 块。安装前备份主配置，Nginx 语法测试通过后重载；未开放回环应用端口。

从 Windows 通过真实公网 IP、禁用 HTTP 环境代理后验证：

- 未认证 API 返回 401；认证后返回应用 0.4.2、remote_mode=true 和正确服务器保存目录。
- 完整 MP4 下载的 SHA-256 与先前成功文件一致；视频 Range 预览返回 206，字节正确。
- 图集 ZIP 可下载，包含完整 12 张图片。
- 错误外部 Origin 和缺失 CSRF 的写请求均返回 403。
- Ghost Chat 回环 HTTP 仍为原有 404，应用仅绑定 127.0.0.1:18765。

这是临时 HTTP 个人体验入口，域名 HTTPS、证书续期和正式切换仍待备案审核通过后完成。访问凭据保存在忽略的 WebData 中，不进入 Git。
