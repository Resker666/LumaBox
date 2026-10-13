# LumaBox · 光匣

通过普通未登录 Chromium 保存一条抖音作品的视频或完整图片集合，并完整解码验证文件。独立 Python 命令行内核；当前没有网站、服务端 API 或部署配置。

## 安装

需要 Python 3.11+，以及 PATH 中可运行的 `ffmpeg`、`ffprobe`。

Windows PowerShell（在本仓库目录）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m lumabox --help
```

Linux 安装命令（云服务器尚未实测）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m playwright install --with-deps chromium
```

## 使用

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249"
.\.venv\Scripts\python.exe -m lumabox --authorized "https://v.douyin.com/p6mTe8JN73M/"
```

可以传完整 `/video/<id>`、`/note/<id>`、短链，或引用起来的分享文案（只含一个 URL）。默认显示浏览器；`--headless` 使用无头模式；`--output` 指定本地保存目录。安装后也可使用 `.\.venv\Scripts\lumabox.exe`。

输出为单行 JSON。成功退出 0，失败退出 1，中断退出 130。视频保存为 `<作品ID>.mp4`；图集保存为 `01.jpg` 等编号文件，并打包 `<作品ID>-images.zip`。每次独立运行有自己的目录和脱敏报告；不会覆盖以前的结果。

只有完整文件全部解码通过才发布成功。图集保留作品顺序和作品中真实重复图片；必须同时确认 DOM 作品 ID、计数器和完整水平轮播的几何位置，过渡状态、循环错位或只加载部分图片时明确失败。图片是网页当前提供的 JPEG，不保证原始上传质量。播放器资源本身可能含水印，本工具没有去水印或重新编码逻辑。

## 网络与会话

程序创建自己的临时匿名会话，无需手工提供账号 Cookie，也不读取你现有浏览器的登录资料；这不保证所有作品都允许匿名访问。遇到明确登录要求、挑战、401/403/429即停止。

不破解签名、不处理验证码、不使用登录账号。浏览器临时本地 CONNECT 代理仅用于校验并固定公网目的地址，不是 VPN；如果 VPN DNS 返回私网/保留地址，程序会返回 `UNSAFE_TARGET`。命令行内核不接受跳过地址检查的选项。

限制：视频 100 MiB、图片 20 MiB、图集 50 张/200 MiB；导航 30 秒、资源等待 45 秒、媒体连接/空闲 15 秒、单文件总下载 180 秒；最多 5 次重定向。

常见错误：`INVALID_INPUT` 输入不支持；`UNSAFE_TARGET` 域名/目的地址/重定向不符合规则；`AUTH_REQUIRED` 需登录；`CHALLENGE_REQUIRED` 访问验证；`BLOCKED` 拒绝访问；`RATE_LIMITED` 限流；`RESOURCE_TIMEOUT` 超时；`UNSUPPORTED_MEDIA` 无法确认完整媒体；`SIZE_LIMIT` 超限；`DOWNLOAD_FAILED` 下载或写入失败；`VERIFY_FAILED` 文件无法完整解码；`DEPENDENCY_MISSING` 浏览器/FFmpeg 安装或文件不可用。

失败 JSON 的 `diagnostics` 记录阶段，例如 `dns_resolution`、`page_navigation`、`media_extraction`、`media_transfer`，并在浏览器阶段提供固定原因及相关域名；不会包含完整签名地址或 Cookie。HTTP 200 只说明主页面有成功响应，不能证明媒体已经提取或下载。被拒绝的子框架仍会阻断并记录，但不会单独使有效主作品失败。正常网络下 HTTP 200 后失败的分析见[诊断记录](docs/validation/2026-10-13-http-200-unsafe-target.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

默认测试不访问抖音：运行真实 Chromium、测试 HTTP 服务和 FFmpeg，使用合成页面及新生成媒体。测试环境需允许浏览器进程和回环网络连接。

此前 Codex 内置浏览器成功下载不算本程序真实下载成功。[2026-10-13 独立 P0 报告](docs/validation/2026-10-13-independent-p0.md)：当前网络返回 `198.18.0.x` 非公网目的地址，两条授权样本均在页面请求前失败；真实下载尚未复现。有界面、无头、Linux 云网络分别验证。

本阶段自行实现内核，没有复制 Resker666/douyin-downloader 的业务代码。来源范围与设计见 `docs/superpowers/specs/`。
