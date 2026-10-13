# 下一次正常网络检查：无头模式

默认有界面 CLI 已在用户正常网络下载并完整解码 1 个视频、12 张图集图片；实际文件、编号、ZIP 和报告已复核。成功运行分别为 `7f6c551d1fcc4f7180a68d6483babe83`、`7731a88238664cdaa9c084158ec9f126`，详见[最新验收](2026-10-13-video-normal-response.md)。无需重复相同的有界面验收。

下一步在相同正常网络、`E:\develop\douyin-downloader\lumabox` 的 PowerShell 测试 `--headless`。这次不会显示 Chromium 窗口，仍使用程序自己的临时匿名会话，不需要登录或提供 Cookie。

先运行视频：

```powershell
.\.venv\Scripts\python.exe -m lumabox --headless --authorized "https://www.douyin.com/video/7686430330920075249" --output Downloaded/p0-headless-video
```

视频成功后运行图集：

```powershell
.\.venv\Scripts\python.exe -m lumabox --headless --authorized "https://v.douyin.com/p6mTe8JN73M/" --output Downloaded/p0-headless-gallery
```

成功条件是 `status=success`、`headless=true`、`checks.full_decode=true`，并产生非空文件；视频 `expected_count=actual_count=1`，图集 `expected_count=actual_count=12` 且包含 ZIP。不要把有界面报告当成无头验收，也不把本地成功外推到 Linux 云服务器。

若失败，保留完整最终 JSON；遇到明确访问拒绝、强制登录或挑战时停止该路径，不连续重试。`detail_response_count` 和 `detail_metadata_parsed` 可帮助判断目标视频详情响应是否出现。

默认 CLI 保留系统 DNS 与公网目的地址校验。如果 VPN DNS 返回非公网映射，仍会在页面前返回 `UNSAFE_TARGET`。不启用此前忽略目录中的诊断解析器，不修改系统网络。

Windows 10054 连接关闭回调仍未修复，最新有界面成功运行也出现过；保留该输出和最终 JSON。不要复制账号 Cookie、媒体签名地址或导出含登录资料的 HAR。网站阶段尚未开始。
