# 下一次正常网络检查

在 `E:\develop\douyin-downloader\lumabox` 的 PowerShell 运行。现有环境为可编辑安装，无需重新安装、账号登录或提供 Cookie。

本轮先只复测图集，验证新的 JPEG/WebP 下载及验证流程：

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://v.douyin.com/p6mTe8JN73M/" --output Downloaded/p0-gallery
```

只有 `status=success`、`expected_count=actual_count=12`、`checks.full_decode=true` 才算该图集真实下载成功。图片扩展名按实际编码确定，可能为 `.jpg` 或 `.webp`；ZIP 中应包括完整 12 张图片及报告。

若失败，提供完整最终 JSON。传输失败现在会报告 `reason`、`resource_order`、`host`、`http_status` 和 `content_type`，可区分不支持的图片类型、访问拒绝和文件问题。旧报告未保留 Content-Type，因此目前不能断言用户上次失败时服务器一定返回了 WebP。

视频暂不重复同一条命令。用户已提供正常网络下的持续 blob 和两组视频 CDN 的 206 响应；这些信息足以否定仅等待 DOM 变成 HTTPS 的通用性。下一步需要在独立程序能够加载该页面的环境中观察正常页面响应，确认目标作品 ID 与完整媒体资源的对应关系，再决定下载完整 MP4 或处理分离音视频。未绑定目标的页面视频、静态宣传素材和特效不得作为成功结果。

当前 Codex 执行环境实测仍在 DNS 阶段因非公网地址映射失败，不能采集这条播放路径的响应正文。此次兼容改动未修复持续 blob 视频，也未通过真实 P0；不会据此开始网站开发。

不要复制账号 Cookie、媒体签名地址或导出包含登录资料的 HAR。若仍出现 Windows 连接关闭回调 10054，保留该输出和最终 JSON；此异常尚未单独修复。
