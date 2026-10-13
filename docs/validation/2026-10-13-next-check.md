# 下一次正常网络检查

在 `E:\develop\douyin-downloader\lumabox` 的 PowerShell 运行。现有环境为可编辑安装，无需重新安装、账号登录或提供Cookie。

先验证图集默认图等待修复：

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://v.douyin.com/p6mTe8JN73M/" --output Downloaded/p0-gallery
```

记录最终JSON。只有 `status=success`、`expected_count=actual_count=12`、`checks.full_decode=true` 才算该图集真实下载成功。若失败，请提供完整最终JSON；不要仅截取错误名。

然后采集视频的正常媒体请求诊断：

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249" --output Downloaded/p0-video
```

视频持续blob尚未解决，仍可能返回UNSUPPORTED_MEDIA。最终JSON的 `diagnostics.media_responses` 会提供正常浏览器实际观察到的媒体响应信息，帮助区分MP4内容、范围请求、分段资源提示或独立音轨。响应头不能证明完整性或目标归属；只有取得目标完整文件并完整解码后才算真实成功。

将这两条命令各自的最终JSON发回即可。不要复制账号Cookie、媒体签名地址或导出包含登录资料的HAR。若仍出现Windows连接关闭回调10054，保留该输出和最终JSON；此异常尚未单独修复。
