# 图集格式与视频正常请求分析

日期：2026-10-13（Asia/Shanghai）。本文件保留格式修补时的调查。后续图集已在用户正常网络成功，视频已在临时公网 DNS 条件下通过正式内核下载；默认网络验收的区别见 [后续视频验证](2026-10-13-video-normal-response.md)。

## 用户正常网络结果

- 图集 `a76797ec31b046629f17b5afa1dc1796`：作品 ID `7695689642767235187`，已解析全部 12 张，失败阶段 `media_transfer`，错误 `UNSUPPORTED_MEDIA`，最终文件为 0。
- 视频 `38a1b0f7a0644a7a90d8a754037c3bbb`：目标区域已确认，`video_class` 绑定正确作品 ID，DOM 源为 blob，失败阶段 `media_extraction`。
- 视频页面正常请求中，`v26-web.douyinvod.com` 有两组 `fetch`、206、`video/mp4` 响应，Content-Range 给出的总大小分别为 843378 和 1631423 字节。另有静态域和特效域的 MP4。

206、Content-Type 和大小尚不能证明这两组请求分别是音轨/视频轨、属于目标作品或能拼成完整文件。没有将任何这类响应直接选作下载资源。

## 图集根因调查与修补

旧传输器根据临时 `.jpg` 文件名，只接受 `image/jpeg` 或通用二进制类型；合法 WebP 被直接拒绝。旧验证器也只认 JPEG 文件头和 `mjpeg` 编码。传输器的这一 MIME 检查是该阶段产生 `UNSUPPORTED_MEDIA` 的来源，但用户原报告没有保留实际类型，不能把 WebP 当作已证实的那次响应类型。

本轮在 Codex 浏览器重新观察同一授权图集：正常请求资产记录包含 12 个 WebP 变体，当前前景 DOM 提供 JPEG 变体。通过媒体路径中同一图像对象的标识核对，两种变体对应这组作品图片。资产导出尝试失败，没有据此取得真实 WebP 响应头或宣称独立程序成功。

已修补：

- 图集传输允许 JPEG、WebP 和已有二进制 MIME；其他类型仍明确失败。
- 依据下载字节识别 JPEG/RIFF-WEBP，向 FFprobe 和 FFmpeg 显式传入对应的 `jpeg_pipe`/`webp_pipe` 解码输入格式，再核对编码与完整解码。不能靠 URL 扩展名或响应 MIME 宣称有效。
- 保留原始字节，验证完成后使用 `.jpg`/`.webp` 正确扩展名；报告及 ZIP 使用同样文件名，维持完整组与顺序。
- 保留固定错误原因；传输失败提供序号、已验证主机、状态及枚举化 MIME。参数和未知 MIME 文本不进入报告，没有输出 Cookie、签名 URL 或响应正文。

未增加新 CDN 域名，未转换或重新编码图片，未开放任意远程 URL。

## 验证

修补前，6 个新增用例按预期失败：WebP 验证、两种 MIME 下混合格式完整 CLI、三种响应类型下失败诊断。

修补后执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_verify.py tests/test_end_to_end.py tests/test_transfer.py tests/test_runner.py -q --tb=short
.\.venv\Scripts\python.exe -m pytest -q --tb=short
```

首次相关 43 项通过；完整 151 项通过，59.69 秒。随后的独立审查仍发现了两个问题，已复现并修正，不能仅凭初次完整通过就忽略其他运行条件：

- `test_webp_is_decoded_and_named_by_bytes_even_when_url_says_jpeg` 在受限进程的临时路径中失败：相同 92 字节 WebP、相同 FFmpeg 构建，自动探测按 `.jpg` 选择 `mjpeg`，尺寸为 0。现已在两个工具中显式选择输入格式；同一受限执行条件下全部 7 项媒体验证测试通过。
- `test_total_deadline_stops_trickle_download` 和 `test_cancel_interrupts_trickled_headers` 补强后失败，表明停止优先级检查产生的新异常丢失了上下文。现已先确定最终错误，再附加已观察诊断。允许本地回环网络条件下，包含这两个分支及完整混合格式 CLI 的相关 17 项通过，10.94 秒。

受限进程中的媒体验证测试不需要回环网络；两个传输测试在该条件下因连接被限制而返回 `DOWNLOAD_FAILED`。它们随后在允许回环网络的测试环境中通过，未把受限环境下的失败当作通过。

测试使用真实 Chromium、本地 HTTP 响应、FFmpeg 生成的真实 JPEG/WebP/MP4 文件，覆盖完整下载、解码、扩展名、报告、ZIP 和失败清理。属于本地集成回归，不能计作真实抖音 P0。

审查修正后，最终再次运行完整命令：**151 passed in 55.94s**，退出 0。独立审查复核了两个修正分支，未发现新的可操作回归。`git diff --check` 通过，`pip check` 无依赖冲突。

## 视频调查与执行环境限制

本轮 Codex 浏览器中目标视频也持续 blob，实际播放器尺寸 1024×576、时长 124.599 秒。已观察到视频 CDN 请求，但资源路径不含目标作品 ID；未发现可直接用于绑定该目标的正常详情请求。原先浏览器成功下载时自然提供完整 HTTPS MP4 的路径，不能外推为所有 Chromium 会话都如此。

执行独立 CLI：

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249" --headless --output Downloaded/agent-20261013-video
```

运行 ID `8a10212748f34a20a05c0045143d96ff`，`UNSAFE_TARGET`，`dns_resolution`，`DNS returned a non-public destination`，未到达页面，HTTP 状态为空。该环境仍不能取得正常播放请求正文；用户正常网络下的 HTTP 200 是另一执行条件。

视频提取策略需要补足正常请求的作品绑定与完整文件处理；单纯加长等待或凭 MP4 MIME 选资源不成立。当前尚未实现这条新路径。没有放宽公网目的地址校验、修改网络配置、导入 Cookie、伪造接口或处理账号挑战。Windows 连接关闭 10054 也没有因为此次日志未出现就被宣称修复。

下一次真实检查只要求用户运行更新后的图集命令，见 `2026-10-13-next-check.md`。视频还需要可到达页面的独立程序环境来确定正常响应中的作品与资源关系；网站阶段保持未开始。
