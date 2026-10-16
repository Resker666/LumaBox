# LumaBox · 光匣

通过普通未登录 Chromium 保存一条抖音作品的视频或完整图片集合，并完整解码验证文件。提供独立 Python 命令行内核和个人使用的本地网页。

当前开发版本为 **0.4.5**（单页动态照片与媒体解析诊断），已发布基线为 [v0.3.0](https://github.com/Resker666/LumaBox/tree/v0.3.0)。[云部署说明](docs/deployment.md)、[云部署验收](docs/validation/2026-10-15-cloud-deploy.md)、[取消与网络诊断验证](docs/validation/2026-10-15-cancel-diagnostics.md)、[日常体验验证记录](docs/validation/2026-10-14-daily-use.md)、[版本变更](CHANGELOG.md)列出范围与验证条件。后续事项见[待办](TODO.md)。

## 当前验证状态

| 场景 | 实际结果 |
|---|---|
| Windows 正常网络，默认有界面视频 | 成功：1920×1080、124.6 秒、H.264/AAC，完整解码通过 |
| Windows 正常网络，默认有界面图集 | 成功：12/12 张，11 WebP + 1 JPEG；逐张解码及 ZIP 核对通过 |
| Windows 本地网页，用户试用视频 | 成功：7696163825922742641，1920×1080、140.133 秒；升级复制后再次完整解码通过 |
| Windows 本地网页，0.3 长视频 | 成功：7695003177908784419，1920×1080、960.149 秒、112.19 MiB；实际文件再次完整解码及哈希核对通过 |
| Windows 本地网页，0.3 图集 | 成功：7695689642767235187，12/12 张 WebP；逐张完整解码、哈希及 ZIP 内容核对通过 |
| 最新无头视频测试 | DNS 返回非公网地址，页面请求前停止；未验证到无头提取/下载阶段 |
| Ubuntu 24.04 云服务器，有界面 Chromium + Xvfb | 命令行视频成功：1920×1080、124.6 秒；图集首次失败，重试 12/12 张成功，均完整解码通过 |
| 云端 systemd 网页后台 | 真实视频、12 张图集及 ZIP、范围预览、取消后队列继续、重启保留历史及文件哈希通过；临时 IP 认证入口已实测，域名 HTTPS 待备案审核通过后启用 |

Windows 10054 连接关闭回调仍有输出；上述有界面视频运行已成功发布文件，这个回调问题尚未修复。当前只确认以上实测样本，不保证所有作品或网络都成功。

## 安装

需要 Python 3.11+，以及 PATH 中可运行的 `ffmpeg`、`ffprobe`。

Windows PowerShell（在本仓库目录）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web,dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m lumabox --help
```

Linux 安装命令（Ubuntu 24.04 云服务器已验证，常驻部署见[云部署说明](docs/deployment.md)）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[web,dev]'
.venv/bin/python -m playwright install --with-deps chromium
```

## 本地网页

完成首次安装后，在 Windows 中双击 [start-lumabox.cmd](start-lumabox.cmd) 即可启动光匣并打开默认浏览器。服务就绪后才打开网页；再次双击会复用同版本、同保存目录的服务。路径包含空格也可使用。其他程序或旧版光匣占用端口时会明确提示，启动器不会结束它们。

关闭浏览器后，后台服务继续运行。运行日志在 WebData/server.log。需要使用控制台管理服务时，可以沿用下面的手动启动方式。

在仓库目录运行：

```powershell
.\.venv\Scripts\python.exe -m lumabox.web
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，粘贴作品链接或分享文案，直接开始下载。本地版无需设置密码、登录或重复勾选授权。单视频默认上限 **1 GiB（1024 MiB）**，可在表单调整为 1～8192 MiB；每个任务独立保存所用上限，重试使用表单当前值。浏览器会记住最后使用的上限。

文件检查通过后已保存在本机 **Output/**，任务卡显示完整保存位置。点击 **打开文件夹** 即可查看视频 MP4、编号图片或图集 ZIP；折叠的 **另存文件** 可再通过浏览器保存一份。文件长期保留，不再按 24 小时删除，也没有默认的 2 GiB 收藏总容量限制。抖音下载仍使用临时匿名浏览器，没有新增抖音账号登录。

Windows 默认显示下载用的 Chromium 窗口，与已实测成功的 CLI 一致。按 Ctrl+C 停止服务；当前运行任务会中断，尚未开始的任务在下次启动时恢复。网页会自动更新状态，无需一直点击刷新。

可选启动方式：

```powershell
.\.venv\Scripts\python.exe -m lumabox.web --port 8766 --data WebData
.\.venv\Scripts\python.exe -m lumabox.web --headless
.\.venv\Scripts\python.exe -m lumabox.web --output Output --video-limit-mib 2048
```

安装网页依赖后也可运行 `.\.venv\Scripts\lumabox-web.exe`；启动无需输入密码。旧 `LUMABOX_WEB_PASSWORD` 不再使用，启动时移除。Windows 支持打开文件夹；云端用 `--remote` 显示下载视频和图集 ZIP 的操作。

网页固定监听本机 `127.0.0.1`，保留 Host、同源 Origin、自动获取的 CSRF 及有界请求体检查；`--remote` 只调整网页呈现，云端通过 `--access-file` 启用光匣登录页，正式域名入口由 Nginx 提供 HTTPS。默认 `WebData/` 保存 SQLite 任务历史，旁边的 `Output/` 保存文件，两者均从 Git 排除。服务启动会核对旧版成功文件哈希，复制到新保存目录，保留原始文件；更改 `--output` 也按同一方式接入历史结果，拒绝覆盖不一致的现有文件。修改 `--data` 会切换到另一份独立任务历史。

限制：同时运行 1 个任务、最多 10 个等待任务；单任务 60 分钟；单文件总下载最多 30 分钟，视频完整解码最多 15 分钟。这些是处理期限，**不限制视频自身时长**。启动任务前根据所选上限、图集打包开销和磁盘余量检查空间；空间不足时明确失败，成功收藏不会因此自动删除。同一数据目录只允许一个服务实例。图集 ZIP 内报告只包含作品及文件校验信息。

失败卡提供一键重试，按当前视频上限建立新任务，保留原失败记录。重复点击同一任务、已有重试仍在等待或运行时拒绝重复提交。超限失败能获得 Content-Length 时显示完整大小；读取中超限仅显示已读取大小下限，不冒充完整大小。

任务列表会显示新下载作品的标题，标题用于搜索；页面没有可确认标题时显示作品 ID，旧记录也可继续使用。标题来自已确认作品区域或同一作品的正常详情响应，不会阻碍媒体下载，也不会自动重新请求旧作品。

运行任务显示“启动浏览器、读取作品、下载媒体、检查文件、整理收藏”等阶段及用时。下载字节来自实际写入，知道完整视频大小时才显示百分比；未知大小显示已接收字节，图集显示已检查数量及当前图片。文件检查和整理阶段显示阶段状态，不显示模拟百分比。

排队或下载中的卡片提供“取消下载”。排队任务立即取消；运行任务显示“正在取消”，停止下载进程并尝试清理未完成文件后结束，队列继续下一项。已取消记录可筛选、移除或“重新下载”；重新下载从头开始，不保留下载断点。已完成任务不能取消，已有收藏文件继续保留。

成功卡提供视频和图集预览。视频可直接播放、拖动进度；图集按编号顺序浏览，支持上一张、下一张和左右方向键。关闭预览会释放播放器。文件已移动或删除时会提示检查保存位置；预览不重新下载抖音资源。

历史支持按标题、作品 ID、链接搜索，按任务状态筛选，并每页显示 20 条。移除已结束任务的记录前会确认，已保存文件保留在 Output；清理失败记录只隐藏失败历史，下载中的任务不能移除。隐藏的记录不再通过网页提供文件，磁盘文件仍可按原路径访问。

已完成本地生成媒体的真实 HTTP/Chromium 网页测试。用户晚间已通过网页成功保存 16 分钟视频及完整 12 张图集，实际文件再次完整解码、哈希及 ZIP 核对通过；该视频超过旧 100 MiB 上限。早间 `UNSAFE_TARGET` 失败记录仍保留；无头模式未验证成功。具体条件见[本地体验改进记录](docs/validation/2026-10-14-local-ux.md)。

## 命令行使用

```powershell
.\.venv\Scripts\python.exe -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249"
.\.venv\Scripts\python.exe -m lumabox --authorized "https://v.douyin.com/p6mTe8JN73M/"
```

可以传完整 `/video/<id>`、`/note/<id>`、短链，或引用起来的分享文案（只含一个 URL）。默认显示浏览器；`--headless` 使用无头模式；`--output` 指定本地保存目录。安装后也可使用 `.\.venv\Scripts\lumabox.exe`。

输出为单行 JSON。成功退出 0，失败退出 1，中断退出 130。视频保存为 `<作品ID>.mp4`；图集保存为 `01.jpg`、`02.webp` 等编号文件，扩展名由实际编码确定，并打包 `<作品ID>-images.zip`。每次独立运行有自己的目录和脱敏报告；不会覆盖以前的结果。

只有完整文件全部解码通过才发布成功。图集保留作品顺序和作品中真实重复图片；必须同时确认 DOM 作品 ID、完整页数和水平轮播的几何位置。页数通常来自计数器；已实测的单页动态照片使用播放器的明确单项标志，不能仅凭一个 DOM 图片推断。过渡状态、循环错位或只加载部分图片时明确失败。

动态照片同时保存图片和该页的 MP4 动态片段（如 `01.webp` 与 `01-motion.mp4`），一起打包、支持网页混合预览。数量按图片页数计算；动态片段受任务的视频上限及图集整组 200 MiB 上限共同约束。背景音乐播放器不属于动态片段，不另行合成音乐。图片支持网页当前提供的 JPEG/WebP，保留原始下载字节，不保证原始上传质量。播放器资源本身可能含水印，本工具没有去水印或重新编码逻辑。

## 网络与会话

程序创建自己的临时匿名会话，无需手工提供账号 Cookie，也不读取你现有浏览器的登录资料；这不保证所有作品都允许匿名访问。遇到明确登录要求、挑战、401/403/429即停止。

不破解签名、不处理验证码、不使用登录账号。浏览器临时本地 CONNECT 代理仅用于校验并固定公网目的地址，不是 VPN；如果 VPN DNS 返回私网/保留地址，程序会返回 `UNSAFE_TARGET`。命令行内核不接受跳过地址检查的选项。

新失败任务会在网页显示可确认的具体网络原因：DNS 返回虚拟或非公网地址、DNS 解析失败或超时、连接被拒绝/重置/超时、网络不可达、TLS 安全连接失败，并提供相关域名和阶段。无法确认原因时保留一般错误提示，不推断为 VPN 故障；旧记录不补造诊断。VPN / 代理兼容已列入[低优先级待办](TODO.md)，本次只改善诊断。

限制：视频默认 1 GiB，可用 `--video-limit-mib` 调整（1～8192 MiB）；图片 20 MiB、图集 50 张/200 MiB；导航 30 秒、资源等待 45 秒、媒体连接/空闲 15 秒、单文件总下载 1800 秒；最多 5 次重定向。

常见错误：`INVALID_INPUT` 输入不支持；`UNSAFE_TARGET` 域名/目的地址/重定向不符合规则；`AUTH_REQUIRED` 需登录；`CHALLENGE_REQUIRED` 访问验证；`BLOCKED` 拒绝访问；`RATE_LIMITED` 限流；`RESOURCE_TIMEOUT` 超时；`UNSUPPORTED_MEDIA` 无法确认完整媒体；`SIZE_LIMIT` 超限；`DOWNLOAD_FAILED` 下载或写入失败；`VERIFY_FAILED` 文件无法完整解码；`DEPENDENCY_MISSING` 浏览器/FFmpeg 安装或文件不可用。

失败 JSON 的 `diagnostics` 记录阶段，例如 `dns_resolution`、`page_navigation`、`media_extraction`、`media_transfer`，并在浏览器阶段提供固定原因及相关域名；不会包含完整签名地址或 Cookie。HTTP 200 只说明主页面有成功响应，不能证明媒体已经提取或下载。被拒绝的子框架仍会阻断并记录，但不会单独使有效主作品失败。正常网络下 HTTP 200 后失败的分析见[诊断记录](docs/validation/2026-10-13-http-200-unsafe-target.md)。

媒体提取诊断还包括 `scope_confirmed`（作品区域确认）、`identity_source`（DOM 身份来源）和 `media_schemes`（媒体协议）。普通登录推广和“登录后发表评论”不算观看限制。若作品播放器持续使用 `blob`，程序也会观察同一主框架正常发出的目标详情响应，核对请求、响应和可见作品的 ID，提取完整 H.264 播放地址；不会主动调用详情接口或下载未绑定作品的媒体响应。缺少这条正常响应或无法确认作品时，仍明确失败。具体证据见[持续 blob 视频的真实验证](docs/validation/2026-10-13-video-normal-response.md)。

图集在加载初期可能让所有前景元素引用同一张 `douyinDefault` 静态默认图。程序识别已实际观察到的默认图并在原45秒资源时限内等待替换；它不能算作作品图片，也不会扩展媒体域名白名单。具体证据见[默认图和持续 blob 分析](docs/validation/2026-10-13-blob-and-gallery-placeholder.md)。

若图集在 `media_transfer` 阶段失败，报告会提供固定错误原因、图片序号、响应主机、状态和脱敏后的 Content-Type；未知类型只记录类别，不输出完整响应头或签名地址。JPEG/WebP 仍须通过文件头、FFprobe 编码和 FFmpeg 完整解码检查。用户正常网络已成功下载并完整解码 12 张图集图片。相关格式修补见[图集格式与视频请求分析](docs/validation/2026-10-13-gallery-format-and-video-requests.md)。

正常详情响应最多读取四份，单份解压后正文最多 2 MiB，使用有容量限制的 Chromium 诊断缓存读取。`detail_response_count` 和 `detail_metadata_parsed` 区分未出现目标详情响应与已解析状态。新增视频 CDN 只允许实际观察到的两个精确 `zjcdn.com` 主机，这两个主机的下载请求不携带 Cookie。

浏览器加载失败的诊断自动包含 `media_responses`：正常页面媒体响应的主机、请求类型、HTTP 状态、内容类型、文件格式提示、是否范围响应、总字节数（响应头可用时）、首个响应样本大小及同组次数。最多保留20组，`media_response_count` 为观察总数，`media_response_truncated` 表示有省略。它们不含完整地址、查询、Cookie或响应正文，只提供下一步分析的证据，不能证明响应属于目标作品或完整文件。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

默认测试不访问抖音：运行真实 Chromium、测试 HTTP 服务和 FFmpeg，使用合成页面及新生成媒体。测试环境需允许浏览器进程和回环网络连接。

Safari 兼容回归使用真实 WebKit，引擎未安装时单独跳过该项。需要完整验证时先运行 `.\.venv\Scripts\python.exe -m playwright install webkit`，再执行测试；手机尺寸的 Chromium 测试不能替代 WebKit 验证。

此前 Codex 内置浏览器成功下载不算本程序真实下载成功。[早期独立 P0 报告](docs/validation/2026-10-13-independent-p0.md) 保留原 DNS 失败记录；[最新视频验证](docs/validation/2026-10-13-video-normal-response.md) 已确认用户正常网络的默认有界面 CLI 成功取得 1080p、124.6 秒的 H.264/AAC 视频，以及完整 12 张图集。实际文件已再次完整解码，图集 ZIP 内容与报告一致。该成功没有启用临时公网 DNS 诊断解析器；返回非公网映射的 VPN DNS 仍会被拒绝。Windows 10054 连接关闭回调仍有输出，尚未修复。Ubuntu 云服务器已通过同样两个作品的有界面 CLI 验证；无头和其他作品需分别实测。网页验证范围见[网页记录](docs/validation/2026-10-13-local-web.md)。

本阶段自行实现内核，没有复制 Resker666/douyin-downloader 的业务代码。来源范围与设计见 `docs/superpowers/specs/`。
