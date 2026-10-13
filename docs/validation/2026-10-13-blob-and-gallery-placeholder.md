# 视频持续 blob 与图集默认图诊断

日期：2026-10-13（Asia/Shanghai）。先记录修复前的原因分析，后续实施记录见末节。真实 P0 未通过。

## 用户正常网络反馈

- 视频运行 `d9e25ad2dd16433ba68541530828a473`（19:49）：HTTP 200，scope_confirmed=true，identity_source=video_class，media_schemes=[blob]；资源等待结束后返回 UNSUPPORTED_MEDIA，没有普通 HTTPS 来源。
- 图集运行 `44a5a8a5e58c4eb984137d54613d1a9f`（19:50）：HTTP 200，scope_confirmed=true，identity_source=note_sidebar；候选主机全部为 lf-douyin-pc-web.douyinstatic.com，返回 UNSAFE_TARGET，没有进入文件下载。
- 两次还出现 Windows asyncio 连接关闭回调的 WinError 10054。

## 已确认的原因

**视频的可行性假设尚未成立。** 最早浏览器下载记录明确写明：currentSrc 先为 blob，随后变为 HTTPS，再由浏览器下载当前 video 资源。原文件仍在 `source/Downloaded/p0-browser/7686430330920075249.mp4`；本轮 FFprobe 复查为 3316797 字节、1920×1080 H.264/AAC、124.6秒。之前的真实成功有效，但不证明所有匿名浏览器会话都会自然提供 HTTPS currentSrc。当前 CLI 只读取 DOM 来源并等待45秒，没有实现对持续 blob 播放路径的媒体获取。

**图集遇到了真实默认图过渡状态。** 本轮正常匿名浏览器只读检查并重新加载授权图集，观察到12个前景元素最初全部使用：

`https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/ies/douyin_web/media/douyinDefault.ee9da4401be3dae0.png`

它们 complete=true、naturalWidth=2416、naturalHeight=1430，随后全部替换为 p3-pc-sign.douyinpic.com 的12张不同尺寸作品图片，尺寸与首次下载记录一致。因此仅检查节点数量、HTTPS或图片已加载，仍不能判断作品资源已经准备好。当前 extract_work 立即校验域名，BrowserSession.load 只等待 UNSUPPORTED_MEDIA，对默认图的 UNSAFE_TARGET 直接退出。这条路径可以解释用户此次图集错误；用户报告没有具体静态路径，因此不能断言其具体文件名必然相同。

**10054发生于连接清理。** 用户堆栈与本地 Python 3.11.9 标准库一致：proactor_events.py 的 _call_connection_lost 中 socket.shutdown 没有捕获该异常。该代码由事件循环回调执行，proxy.py 围绕 await writer.wait_closed 的 OSError 捕获不能覆盖这个回调。两个最终错误都发生在媒体提取阶段；该额外异常不能直接解释 blob 或默认图。当前证据没有连接对端信息，不能断言是抖音主动封禁或关闭了哪条连接。

## 下一步应验证的方案

1. 图集明确识别已观察到的默认图状态并有界等待替换；持续默认图明确失败。不把这个静态主机全局加入媒体白名单，否则可能把12份默认图保存为作品。
2. 视频需要观察标准 Chromium 为目标播放器实际发出的正常媒体请求，再验证资源与作品的对应关系、是否完整文件、是否分段及是否独立音轨。不能仅凭页面中存在一个 CDN 请求就认定它是目标作品，也不能把 blob 字符串交给 HTTP 下载器。该路径仍需要真实证据和独立完整解码验收。
3. 单独复现和处理 Windows 连接清理异常；不能简单全局吞掉 ConnectionResetError，也不能用它替代媒体提取诊断。

本轮执行环境的 www.douyin.com、v.douyin.com 公网解析校验仍返回非公网目的地址，因此不能用当前 CLI 网络直接观察用户 HTTP 200 后的真实媒体请求。浏览器只读观察不算 CLI 真实下载成功。此前132项离线测试没有覆盖默认图替换，也不证明真实平台兼容；本轮未重复这些测试。

## 后续实现

分析后已补充已观察默认图的精确识别：它返回可等待的 UNSUPPORTED_MEDIA 状态，不下载默认图，也不放行其他静态资源。全部作品图片准备好后才返回完整资源；持续默认图在原有时限内失败，混合默认图和其他未验证主机仍拒绝。

持续blob失败报告已增加有界的正常媒体响应诊断，便于用户在可访问页面的网络上提供媒体类型、状态、范围和总大小信息。只观察标准浏览器正常响应，不读取响应正文或记录完整地址，不自动选取这些响应为目标视频。具体复测步骤见[下一次检查](2026-10-13-next-check.md)。视频持续blob的下载路径、真实P0和Windows关闭回调异常仍未解决。

验证：默认图回归先有4项失败，媒体响应诊断先有5项失败；对应修复后各项通过。完整离线测试 **144 passed in 50.01s**，退出0，pip check无依赖冲突。新增12项覆盖默认图替换/持续默认图/混合状态、其他未验证主机继续拒绝，以及真实Chromium响应诊断、范围、聚合上限、子框架排除和脱敏。该验证没有替代真实平台下载验收。
