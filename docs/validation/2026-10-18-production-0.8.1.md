# 0.8.1 sf11 / sf6 配乐节点修复与云端验证

2026-10-18，用户报告云端配乐再次提示域名错误。正式服务已从 `247fcd6` / 0.8.0 升级到 `eb0fe42` / **0.8.1**，并对原失败任务补保存配乐成功；本轮没有推送 GitHub。

## 根因与实际节点证据

云端任务 `f1396ec34e2c412f92f479112ccc282a` 对应作品 `7695353019219257009`，主图文成功保存 1 张图片及 ZIP，音频单独失败。诊断为 `UNSAFE_TARGET` / `AUDIO_HOST_UNSUPPORTED`，阶段为 media_extraction，准确域名为 `sf11-cdn-tos.douyinstatic.com`。这是现有音乐来源列表未覆盖的节点。

随后用普通未登录浏览器重新打开同一作品，已确认的唯一背景播放器换到 `sf6-cdn-tos.douyinstatic.com`，因此只补 sf11 无法覆盖本次实际切换。隔离探针只临时接受这两个精确候选，保留 HTTPS 语法、逐跳公网 DNS、固定地址连接、TLS、响应长度及 MP3 校验。使用已确认播放器的同一对象路径和查询分别验证两个实际出现的节点：

| 节点 | 响应与实际文件 |
|---|---|
| sf11-cdn-tos.douyinstatic.com | HTTP 200，audio/mpeg，463,031 字节，28.760813 秒 |
| sf6-cdn-tos.douyinstatic.com | HTTP 200，audio/mpeg，463,031 字节，28.760813 秒 |

两个文件 SHA-256 均为 `f0e18036130f34defecff12cd53b718e701c9c8a7ef11dba0a89c0b4848db8dc`，完整解码与实际解码时长核对通过。未转码，未保存签名地址或 Cookie。

补丁只为音频增加这两个精确来源，继续支持原来的 lf26、lf9 和已验证的重定向目标。原图片 / 视频域名范围不变，未放开整个 douyinstatic.com 或所有编号节点；未知节点继续给出具体诊断。

## 回归与审查

- 新回归先运行得到 **4 failed、1 passed**，失败分别发生在 sf11 / sf6 的播放器提取及重定向下载，原因均为旧列表拒绝。
- 修复后，节点、音频诊断及原 MP3 核心检查 **62 passed、1 warning，12.05 秒**。
- 完整命令 `.\.venv\Scripts\python.exe -m pytest -q --tb=short --junitxml=Downloaded/music-host-fix/windows-full.xml`，结果 **733 passed、1 skipped、1 warning，470.64 秒**。
- 固定提交的独立 Linux 候选环境，使用服务器现有完整 Chromium 执行无界面测试，覆盖图集、节点、诊断、原音频、批量、判重和 URL：**186 passed、1 warning，55.14 秒**。完整 Windows 回归与云端候选检查并行进行，最终均通过。
- 独立只读审查没有遗留问题。跳过项和 Starlette TestClient / httpx 弃用警告沿用既有条件，没有为本次补丁修改其他功能或依赖。

## 正式升级与原失败任务补保存

正式服务空闲后停止，备份 SQLite、完整 Output、旧提交及所有任务 / 索引 / 文件 / 密码哈希。备份在 `WebData/deployments/2026-10-18-0.8.1-eb0fe42/`；用原虚拟环境离线安装，保持 dev 用户、原端口、Xvfb、Nginx、访问密码及 1100 MiB 内存限额，没有数据库结构变更。启动后核对原 **25 条任务、47 个文件索引、60 个输出文件哈希**全部一致。

通过正式公网接口仅对上述失败任务请求补保存音频，没有新建作品任务或重新下载图片。第一次补保存未进入媒体下载，返回 `DOWNLOAD_FAILED`，诊断为 page_navigation / `www.douyin.com` / `CONNECTION_TIMEOUT`；保留该实际失败记录。随后只重试一次，原任务配乐成功。

正式附件为原始 MP3 463,031 字节、28.760813 秒，SHA-256 与上述两个节点的 P0 文件相同。公网下载后再次完整解码，核对实际时长；audio/mpeg 范围预览返回 206，字节与文件前 64 字节一致。390×844 Chromium 实际下载、试听、拖动进度及页面布局检查通过。

最终仍为 **25 条任务**，索引增至 **48 个**，输出文件增至 **61 个**。所有旧索引与 60 个原文件哈希保留，密码文件哈希一致；其他任务的全部字段保持不变。仅目标任务的音频状态 / 归属与更新时间按补保存变化，旧错误详情已清除，新增一个 MP3 索引及文件。原图片、ZIP、报告及主任务字段保持不变。

最终服务 active / running，`NRestarts=0`，主下载与音频活动均为 0；观测 MemoryPeak 为 982,466,560 字节，MemoryCurrent 约 535 MB，max / oom / oom_kill 均为 0。Nginx active，没有修改其他站点。本次未触发回退。

## 范围与证据

本轮确认上述单图作品与两个精确音乐节点。iPhone Safari 0.8.1 实机尚待用户体验；没有重测 Windows WebKit，继续保留 [0.8.0 浏览器边界记录](2026-10-18-production-0.8.0.md)。VPN 兼容和用户此前暂缓的稳定性工作没有扩展。

服务器备份目录保存 before / after-start / after-validation 快照，候选目录为 `/home/dev/develop/lumabox-0.8.1-validation-20261018`。本地忽略目录 `Downloaded/music-host-fix/` 保存脱敏 P0、bundle、部署和回退脚本、完整回归 JUnit、Linux 日志、补保存结果、MP3 下载副本及 Chromium 截图。数据库、密码、Cookie 和签名 URL 不加入 Git。
