# 0.6.0 正式云服务上线记录

日期：2026-10-17。用户确认本地修复后要求部署云端，将正式服务从 `b7b61c2` / 0.5.1 升级至 `3f2e95b` / **0.6.0**，包含手机界面、可选视频音频以及 Windows 下载结果编码修复。

## 部署与数据保留

- 正式路径为 `/home/dev/develop/lumabox`，继续以 dev 运行；仍由 systemd 启动 Xvfb 与服务，应用只监听 `127.0.0.1:18765`。公网入口继续为 `http://39.106.202.240/`，原访问密码保持。
- 部署前确认无活动任务、正式源码无未提交修改。通过本地 Git bundle 传输明确版本，没有推送 GitHub。
- 先在 `/home/dev/develop/lumabox-0.6-validation-20261017` 独立验证，复用服务器已有 Python 3.12、FFmpeg 6.1.1 及依赖。`test_audio_extract.py`、`test_audio_jobs.py`、`test_result_encoding.py`（排除两种 UI 浏览器用例）共 **79 passed, 2 deselected, 1 warning，23.08 秒**。警告为已有 Starlette TestClient / httpx 弃用提示。
- 停止正式服务后，用 SQLite backup 生成一致快照并完整备份 Output，记录旧提交、历史行、文件索引、全部输出 SHA-256 和访问凭据文件哈希。备份位于 `WebData/deployments/2026-10-17-0.6.0-3f2e95b/`，包含 `before.json`、`jobs-before.sqlite3`、`Output-before.tar`。
- 使用原虚拟环境无网络、无依赖升级地重新安装 editable 包，启动并确认认证后版本为 0.6.0，再核对增量音频字段。保留 systemd、Nginx、1100 MiB 内存限制及原密码。
- 启动后及实际验收后各核对一次：原 **12 条任务、23 个文件索引、29 个输出文件哈希** 全部保持；没有用空数据库覆盖历史，也未删除原有文件。回退步骤已准备，本次未触发回退。

本地编码回归和完整测试的详细结果保留在[编码修复记录](2026-10-17-windows-result-encoding.md)，包括首次全量运行的一项既有 WebKit 超时及随后整个模块 12 项复测通过；没有把该次全量运行记为全绿。

## 公网真实验收

在真实 IP 入口使用 390×844 手机尺寸浏览器，不绕过认证或 CSP：

| 检查 | 实际结果 |
|---|---|
| 原密码登录、匿名访问保护 | WebKit 与 Chromium 登录通过；未登录 API 为 401 |
| 手机界面和选项 | 首页无横向溢出；下载选项默认折叠，音频默认不勾选 |
| 实际新下载 | 通过页面勾选音频，提交作品 `7676043714435897019`，视频与音频均成功 |
| 浏览器文件下载 | WebKit 与 Chromium 完成 MP4、M4A 下载，字节数和 SHA-256 与服务器索引一致 |
| 范围预览 | 视频与音频均返回 206，前 64 字节与实际文件一致 |
| Chromium 音频试听 | 时长 18.69 秒，实际播放、拖动至 5 秒后继续、关闭后暂停并移除 src 均通过 |
| Windows WebKit 音频试听 | 本条 HE-AACv2 音轨报媒体不支持，readyState=0、error=4，未通过 |
| 来源与脚本检查 | 外部 Origin 写请求返回 403；最终 Chromium 页面无脚本错误 |

正式任务 `3b02ef7394c3477e8b5a626d883307de` 保留供用户查看、下载，音频附在同一收藏下。

| 文件 | 字节数 | SHA-256 |
|---|---:|---|
| MP4 | 2,909,945 | `b9e7d6674a8da7abd046fbd6f7344599e7e10e765019ab3dca0d3ac025ed0e6d` |
| M4A | 147,605 | `345420069aa7ac4f8c8e4b57fe7b9fad524e73bbe5851e21307035384329114f` |

本条云端源视频音轨为 HE-AACv2，与此前本地 AAC-LC 样本不同。M4A 复制原音轨，FFmpeg 完整解码及文件哈希已通过；Chromium 可播放。Windows 上的 WebKit 自动化不代替 iPhone Safari 真机，不能据此宣称该样本在真实 iPhone 上已经通过或必然失败，真机试听列入待办。

验收脚本最初对播放前时长以及关闭后的 currentSrc 使用了过强假设，另有一处 Playwright 断言参数错误；修正为点击播放后读取时长、关闭后核对暂停及移除 src，完成最终 Chromium 检查。没有修改产品代码或放宽文件检查。

## 运行状态与证据

最终服务为 active / running，`NRestarts=0`，没有活动下载或音频任务；当前共 13 条历史、25 个文件索引。服务内存峰值 1,153,449,984 字节，内存上限触发回收 max=1504，oom=0、oom_kill=0；验收后内存约 500 MB，未提高限额。

服务器的 `after-validation.json` 保存在上述私有备份目录。Linux 针对性日志在候选目录 `Downloaded/cloud-validation/`；公网结果、下载副本、浏览器诊断与截图位于本地忽略目录 `Downloaded/cloud-0.6.0/`。凭据、Cookie、CSRF、下载媒体及数据库均未加入 Git。

本轮没有调整域名 HTTPS 或其他站点。服务重启会使旧会话失效，刷新后用原密码重新登录即可。用户真实 iPhone 试听体验及 GitHub 推送等待后续反馈或指示。
