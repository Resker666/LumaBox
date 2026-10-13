# 下一次正常网络检查

在 `E:\develop\douyin-downloader\lumabox` 的 PowerShell 运行。现有环境为可编辑安装，无需重新安装、账号登录或提供 Cookie。

图集已经由正常网络成功验证 12 张，下一步只复测新接入的正常详情响应视频路径：

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249" --output Downloaded/p0-video
```

只有 `status=success`、`expected_count=actual_count=1`、`checks.full_decode=true`，并产生非空 MP4，才算本次默认 CLI 视频成功。此样本已在临时公网 DNS 条件下通过正式内核取得 1920×1080、124.6 秒、H.264/AAC 的完整文件；不能外推到所有作品或网络。

若失败，提供完整最终 JSON。`detail_response_count` 和 `detail_metadata_parsed` 可判断正常详情响应是否出现并解析；传输阶段的 `reason`、`host`、`http_status` 和 `content_type` 可区分媒体连接、访问拒绝和格式问题。

此次命令使用已修补的提取路径，不再只等待 blob 变为 HTTPS。程序仅观察页面本来就发出的详情请求，并核对目标作品，下载完整 MP4；不从未绑定的范围请求、推荐或特效素材中猜测目标，也没有增加分离音视频拼接功能。

默认 CLI 保留系统 DNS 与公网目的地址校验。如果使用返回非公网映射的 VPN DNS，仍会在页面前返回 `UNSAFE_TARGET`。真实下载及条件区别见 [视频正常响应验证](2026-10-13-video-normal-response.md)；本轮未修改系统网络或发布诊断解析器，网站阶段尚未开始。

不要复制账号 Cookie、媒体签名地址或导出包含登录资料的 HAR。若仍出现 Windows 连接关闭回调 10054，保留该输出和最终 JSON；此异常尚未单独修复。
