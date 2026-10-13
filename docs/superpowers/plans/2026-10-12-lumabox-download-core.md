# LumaBox 独立下载内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 交付脱离 Codex 可运行的 `lumabox` 命令行，通过普通未登录 Chromium 下载一条授权视频或完整图集，并用真实样本验证。

**Architecture:** 使用标准 Playwright Chromium 读取正常作品页面的 DOM。浏览器通过本次运行专用的本地 CONNECT 代理连接已校验的公网 IP，媒体通过共享网络策略的流式 HTTPS 客户端下载；全部验证通过后才发布文件。文件验证和 CLI 编排独立于浏览器，不导入原仓库业务代码。

**Tech Stack:** Python 3.11+；Playwright 1.58.0；pytest、pytest-asyncio；系统 FFmpeg/FFprobe；Python 标准库网络、文件和 ZIP 工具。

**Spec:** [独立浏览器下载内核设计](../specs/2026-10-12-standalone-browser-download-design.md)

## Global Constraints

- 项目名称 **LumaBox · 光匣**，仓库、Python 包和命令行均为 `lumabox`。在新仓库当前分支 `feat/standalone-browser-download` 实施，原 `source` 仓库仅作参考。
- Python 3.11+；标准 Chromium；默认有界面，可选 `--headless`。不隐藏自动化特征、不导入账号 Cookie、不登录、不破解签名、不解验证码。
- 单作品、单次匿名会话；完整 `/video/<id>`、`/note/<id>`、短链、含单一链接的分享文案。
- 页面精确域名：`douyin.com`、`www.douyin.com`、`v.douyin.com`、`iesdouyin.com`、`www.iesdouyin.com`；仅 HTTPS 默认端口；最多 5 次重定向。
- 初始媒体根域：`douyinvod.com`、`douyinpic.com` 及其严格子域；禁止本机、私网、保留和非公网目的地址；不放宽检查适配 VPN DNS 映射。
- 导航 30 秒、资源等待 45 秒、媒体连接/空闲 15 秒、每文件总时限 180 秒；视频 100 MiB、图片 20 MiB、图集 50 张/200 MiB。
- 对明确 401、403、429、挑战或强制登录停止；普通登录推广不自动算作强制登录。失败不换接口、不重试绕过。
- 成功必须完整解码；图集保留作品顺序及作品中的真实重复图片；ZIP 只收录当前成功图片和报告。失败清理自己的临时文件，保留报告。
- 输出不含 Cookie、鉴权头、完整媒体签名 URL、原始页面正文和令牌；文件名只用程序生成的作品 ID、运行 ID 和序号。
- 不开发网站、不创建远程仓库、不推送、不部署。只有独立真实下载成功，才制定网站阶段计划；云网络另验。

## Review Focus

- 分享文案有多个 URL、编码控制字符或伪装域名：拒绝含糊输入，不猜测要下载哪条（Task 1）。
- DNS 同时返回公网和私网，或校验后重绑定：拒绝混合结果，连接固定已校验 IP（Task 2）。
- 重定向没有再次经过 Playwright route，或弹出新页面：文档请求逐跳检查，关闭额外页面（Task 3）。
- 轮播虚拟化、背景副本、真实重复图片：数量/序号无法确认就失败，不能输出看似完整的半组（Task 3）。
- 中断、磁盘写入失败、解码失败或 ZIP 失败：不发布成功，不删除以前的结果，退出非零（Task 5）。

## 文件和接口地图

- `pyproject.toml`、`.gitignore`、`src/lumabox/__init__.py`：包、开发依赖、入口和忽略运行产物。
- `src/lumabox/models.py`：`InputLink(url, work_id, kind)`、`MediaResource(url, order)`、`Work(work_id, kind, resources)`、`VerifiedFile(name, bytes, sha256, width, height, duration)`；kind 为 `video` / `gallery`，短链输入未解析时为 `None`。
- `src/lumabox/errors.py`：`LumaBoxError(code: str, message: str)`，message 只用固定脱敏文本。
- `src/lumabox/urls.py`：输入提取、页面/媒体 URL 策略与最终作品 ID。
- `src/lumabox/net.py`、`proxy.py`：公网解析、固定 IP 的 HTTPS 连接、仅本次运行可用的 CONNECT 代理。
- `src/lumabox/browser.py`、`extract.py`：会话和访问状态；已确认作品区域的 DOM 媒体提取。
- `src/lumabox/transfer.py`、`verify.py`：流式下载、容量限制、完整解码及结果元数据。
- `src/lumabox/runner.py`、`cli.py`、`__main__.py`：原子发布、失败报告、退出状态和 CLI。
- `tests/`：单元测试、合成 DOM/本地网络夹具和完整 CLI 测试。生产配置没有跳过安全校验的测试开关。
- `README.md`、`docs/validation/2026-10-13-independent-p0.md`：安装/运行说明和独立实验实际结果。

### Task 1: 输入规则、包和公共契约

**Files:** 创建 `pyproject.toml`、`.gitignore`、`src/lumabox/{__init__,models,errors,urls}.py`、`tests/test_urls.py`。

**Interfaces:** 产出 `parse_input(text: str) -> InputLink`、`validate_page_url(url: str) -> str`、`validate_media_url(url: str) -> str`；错误类型和数据模型按文件地图定义。

- [x] 写失败测试。合法视频/图文/短链、中文分享文案和查询参数应解析；两个 URL、HTTP、用户名、非 443 端口、伪装子域、反斜杠、百分号编码换行、无效路径和非 15～20 位作品 ID 应拒绝。例如：
  ```python
  assert parse_input("https://www.douyin.com/video/7686430330920075249").work_id == "7686430330920075249"
  with pytest.raises(LumaBoxError, match="INVALID_INPUT"):
      parse_input("https://www.douyin.com.evil.test/video/7686430330920075249")
  ```
- [x] 创建本地 `.venv` 并安装开发依赖；`python -m pytest tests/test_urls.py -q` 首次因缺少实现失败，确认失败原因。
- [x] 实现上述函数。剥离分享文案的尾部标点，拒绝控制字符/混淆和多个链接；短链可无作品 ID，最终页必须有明确作品 ID。包使用 `src` 布局、`lumabox=lumabox.cli:main` 入口；忽略 `.venv/`、缓存、`Downloaded/`。
- [x] 执行 `python -m pytest -q`，全部通过；提交 `feat: define LumaBox input and media contracts`。

### Task 2: 公网连接和浏览器网络边界

**Files:** 创建 `src/lumabox/net.py`、`src/lumabox/proxy.py`、`tests/test_network.py`、`tests/test_proxy.py`、`tests/helpers/network.py`。

**Interfaces:** 产出 `resolve_public(host: str) -> tuple[str, ...]`、`PinnedHTTPSConnection(host: str, ip: str, timeout: float)`；`PublicProxy` 异步上下文管理器产出本地 `server_url: str` 与脱敏 `failures`，退出关闭全部隧道。

- [x] 写失败测试：纯公网成功、空 DNS/混合地址/IPv4 映射私网拒绝、固定连接目标与 TLS 原域名一致；CONNECT 拒绝非 443、凭据、IP 字面量私网和非法请求，且从未连接目标。独立测试夹具只替换解析/底层拨号以观察真实代理数据流，不放宽生产策略。例如：
  ```python
  with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
      resolve_public("mixed-addresses.test")  # 夹具返回 93.184.216.34 和 127.0.0.1
  assert received_tunnel_bytes == b"fixture payload"
  ```
- [x] 执行 `python -m pytest tests/test_network.py tests/test_proxy.py -q`，确认功能缺失导致失败。
- [x] 实现固定 IP 连接并保留 TLS/SNI 证书校验；代理只绑定回环随机端口、只接受 CONNECT、只连接经校验公网 IP:443、不解密 TLS、不接受外部代理配置。限制 CONNECT 头 16 KiB、连接 15 秒；停止时取消并关闭存活连接。
- [x] 执行全套 `python -m pytest -q`，通过后提交 `feat: guard and pin outbound network connections`。

### Task 3: 普通浏览器与作品资源提取

**Files:** 创建 `src/lumabox/browser.py`、`src/lumabox/extract.py`、`tests/test_browser.py`、`tests/test_extract.py`、`tests/fixtures/*.html`。

**Interfaces:** 消费 Task 1、2；产出 `BrowserSession(headless: bool)` 异步上下文管理器、`await session.load(link: InputLink) -> Work`、`await session.media_headers(url: str) -> dict[str, str]`。headers 只含当前程序创建的匿名会话中针对该 URL 的必要头，禁止日志输出。`extract_work(snapshot: dict, expected_id: str) -> Work` 消费 DOM 快照。

- [x] 写失败测试：blob 后自然变 HTTPS、目标 ID 不匹配、其他视频/头像/推荐图排除、背景副本排除、真实重复图片保留、乱序恢复、无法确认完整数量失败、51 张失败。合成页面必须包含推荐区域和背景副本，不能只有测试需要的一个元素。例如：
  ```python
  work = extract_work(gallery_snapshot, "7695689642767235187")
  assert [r.order for r in work.resources] == [1, 2, 3]
  assert work.resources[0].url == work.resources[2].url  # 两张真实重复作品图片仍保留
  ```
- [x] 写浏览器测试：逐跳导航拒绝外域/第六次跳转、挑战/401/403/429 停止、普通推广且有媒体继续、强制登录停止、45 秒等待耗尽、上下文退出清理。用本地合成响应运行真正 Chromium；网络拨号隔离在测试夹具，不访问抖音。
- [x] 执行 `python -m playwright install chromium`，再执行 `python -m pytest tests/test_browser.py tests/test_extract.py -q`，确认实现缺失导致失败。
- [x] 实现标准 Chromium、非持久匿名 context、代理及受控文档导航。使用公开 CDP Fetch 文档请求拦截逐跳检查；不能假定 `context.route()` 会再拦截浏览器的每次重定向。阻止新窗口、HTTP、代理回环绕过和直接 UDP；保留正常 TLS 验证。默认导航 30 秒，资源等待 45 秒，禁止网络错误原文/签名 URL进入输出。
- [x] 实现只读 DOM 快照：确认最终作品 ID、目标作品根区域和播放器/轮播结构。以此前观察的轮播结构为起点，记录真实 DOM 证据后确定选择器；不能只依赖随机类名。根据 slide 序号与总数验证完整性，必要时正常点击下一张加载余下图片；无法证明完整则失败。明确挑战/强制登录立即停止；blob 超时返回 `UNSUPPORTED_MEDIA`。
- [x] 执行全套 `python -m pytest -q`，通过后提交 `feat: extract target video and complete gallery in Chromium`。

### Task 4: 有界下载和完整解码

**Files:** 创建 `src/lumabox/transfer.py`、`src/lumabox/verify.py`、`tests/test_transfer.py`、`tests/test_verify.py`、测试媒体夹具生成工具。

**Interfaces:** 消费 Task 1、2；产出 `download_resource(resource: MediaResource, path: Path, headers_for_url: Callable[[str], dict[str, str]], max_bytes: int) -> int`；`verify_file(path: Path, kind: str) -> VerifiedFile`。阻塞网络/FFmpeg 操作由 runner 通过 `asyncio.to_thread` 调用，保持浏览器事件循环活跃；runner 的头部回调用 `asyncio.run_coroutine_threadsafe(session.media_headers(url), loop).result(timeout=15)` 在原事件循环读取对应匿名会话头。

- [x] 写失败测试：允许域名重定向和私网重定向拒绝、六次跳转拒绝、声明/流式大小超限、空内容、Content-Length 不符、慢读空闲/总超时、401/403/429分类；本地测试服务返回实际字节，不能只断言模拟函数调用。
- [x] 写真实 FFmpeg 测试夹具：生成小 MP4+音频、JPEG、截断 MP4、假扩展名 HTML；有效文件获得正确尺寸/字节/哈希，损坏文件 `VERIFY_FAILED`。例如：
  ```python
  result = verify_file(valid_mp4, "video")
  assert (result.width, result.height) == (64, 48)
  with pytest.raises(LumaBoxError, match="VERIFY_FAILED"):
      verify_file(truncated_mp4, "video")
  ```
- [x] 执行 `python -m pytest tests/test_transfer.py tests/test_verify.py -q`，确认失败。
- [x] 使用固定 IP 的 HTTPS 连接逐跳流式下载，分块 64 KiB；逐跳重新取得当前匿名会话对应头，跨域不能沿用 Cookie。15 秒连接/空闲和 180 秒总时限，独占创建目标临时文件，失败删除本文件；只接受完整 200 与对应 MP4/JPEG类型或需验证的二进制类型，不把 206 片段算作完整文件。
- [x] FFprobe 确认 MP4/视频流或 JPEG，FFmpeg `-v error -xerror -nostdin` 完整解码视频及已有音频/单张图片，工具执行最大 180 秒。检查格式签名、非空、尺寸与时长，输出 SHA-256。依赖缺失用清晰固定错误信息，不传播 subprocess 输出中的路径/资源。
- [x] 执行全套 `python -m pytest -q`，通过后提交 `feat: stream and verify media before publication`。

### Task 5: CLI、原子产物和失败报告

**Files:** 创建 `src/lumabox/{runner,cli,__main__}.py`、`tests/test_runner.py`、`tests/test_cli.py`，更新 `README.md`。

**Interfaces:** 消费前四任务；产出 `async run_download(text: str, output: Path, headless: bool) -> dict`、`main(argv: list[str] | None = None) -> int`；`python -m lumabox` 和安装后的 `lumabox` 等价。

- [x] 写失败测试：视频成功发布、完整图集编号/ZIP成功、第二张失败无半组发布、200 MiB 超限、中断/磁盘写入/验证/ZIP失败、之前成功目录不受影响、所有异常结果脱敏、CLI 非零退出。例如：
  ```python
  assert result["status"] == "failed"
  assert list(output.glob("*/02.jpg")) == []
  assert previous_success.read_bytes() == b"previous result"
  assert "signature=" not in json.dumps(result)
  ```
- [x] 执行 `python -m pytest tests/test_runner.py tests/test_cli.py -q`，确认失败。
- [x] 实现 `lumabox INPUT --authorized [--output Downloaded] [--headless]`；授权标志为必填确认，默认有界面，一次运行一个作品。创建 UUID 运行 ID和输出目录内 `.partial-<run-id>`；视频 `<id>.mp4`，图集 `01.jpg`…；计入每个作品位置的文件大小，不能按哈希删掉真实重复图片。
- [x] 成功报告含 ID/类型/metadata_parsed/expected_count/actual_count/files/bytes/checks/status；全部下载及完整解码通过后生成 gallery ZIP（编号图片+报告），同卷 rename 为 `<id>-<run-id>`。失败仅清理自己的临时目录，保存 `<run-id>-failed.json`；Ctrl+C 清理并退出 130；其他失败退出 1；成功退出 0。失败报告也写不了时 stderr 返回脱敏错误且非零。
- [x] README 记录 Windows/Linux `.venv`、包安装、Chromium 安装、FFmpeg、命令、限制和错误含义；明确 Cookie 无需手工导入与所有作品可用是不同命题，Linux/headless 未实测不宣称支持成功。
- [x] 执行全套 `python -m pytest -q` 与 `python -m lumabox --help`，通过后提交 `feat: publish verified downloads through LumaBox CLI`。

### Task 6: 独立 P0 实验与交付

**Files:** 创建 `tests/test_end_to_end.py`、`docs/validation/2026-10-13-independent-p0.md`；更新 README 和此计划执行状态。真实媒体存 `Downloaded/`，不进 Git。

**Interfaces:** 使用正式 CLI 和真实工具；实验不使用 Codex 浏览器下载能力、不复用此前成功媒体。

- [x] 完整合成页面→提取→实际字节下载→FFmpeg验证→发布，视频/图集各一次；加入真实 HTTP失败的端到端测试，assert 非零退出、错误报告、无成功文件。执行全套 `python -m pytest -q`。
- [x] 记录 Python/Playwright/Chromium/FFmpeg 版本、日期、headed/headless、网络条件可确认事实。首先运行 `python -m lumabox --authorized "https://www.douyin.com/video/7686430330920075249" --output Downloaded/p0-video`。
- [x] 视频成功才运行 `python -m lumabox --authorized "https://v.douyin.com/p6mTe8JN73M/" --output Downloaded/p0-gallery`；若视频被 DNS/访问限制挡住，仍可诊断图集的相同网络阶段，但不得突破停止规则或宣称真实下载已实现。
- [x] 成功必须产生本次新 MP4及全部 12张图片，独立检查视频尺寸/时长/音频、图集顺序/数量/逐张解码和 ZIP 完整性；更新结果报告。用户正常网络默认有界面视频运行 `7f6c551d1fcc4f7180a68d6483babe83`、图集运行 `7731a88238664cdaa9c084158ec9f126` 均成功，实际文件再次完整解码和报告核对通过，ZIP CRC、编号/成员顺序及哈希一致。
- [ ] 有界面成功后验证 `--headless`；云服务器留待单独授权和网络验证。最新无头运行 `d8748dcbfa9a4be1b54c970394e70ef3` 在 DNS 非公网结果阶段停止，未到页面。用户要求先整理当前版本供 GitHub 推送审阅，不继续追查；可选复测命令保留在 `docs/validation/2026-10-13-next-check.md`。
- [x] 最终代码修补后完整测试 179 项通过，`git diff --check` 通过，独立审查重要发现已修复并复跑相关和全套测试；随后完成默认有界面真实下载产物复核并更新交付记录。Windows 10054 关闭回调仍单独待修，不把它或无头/云验收标为已完成。

## 计划自审

输入、匿名浏览器、目标匹配、完整图集、blob等待、网络目的地址/重定向/超时、下载容量、解码、原子发布、失败退出、脱敏、复用许可和真实验收均有对应任务；本阶段不复制第三方业务代码。离线与真实实验分开，安装缺失和真实访问失败都不能以测试夹具成功代替。

技术依据：[Playwright 网络与代理](https://playwright.dev/python/docs/network)、[Chromium Fetch 文档请求拦截](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)、[Playwright CDP 会话](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-new-cdp-session)。特别注意官方网络文档说明浏览器重定向不会重新触发同一 route，地址校验不能只放在 route 回调里。

执行方式：在当前新仓库由本会话直接实施，任务末尾跑完整测试并提交，最终做一次独立代码审查。用户已确认设计及计划并指示“执行”；2026-10-12/13 已实现独立 CLI。首次真实实验因系统 DNS 返回非公网目的地址未通过，原始诊断已保留；最新用户正常网络结果确认默认有界面视频及图集通过，未进入网站阶段。

后续（2026-10-13）：视频补足同一主框架正常详情响应的作品绑定与完整 H.264 地址提取后，先有临时公网 DNS 条件下的正式内核成功，最终修补版本又由用户在正常网络通过默认 CLI 下载并完整解码 1080p H.264/AAC MP4；12/12 图集及 ZIP 也已通过真实产物复核。详见 `docs/validation/2026-10-13-video-normal-response.md`。Task 6 的默认有界面样本验证完成，无头/云验收仍待完成，网站阶段未开始。

本轮收尾：用户要求整理已有版本用于 GitHub 仓库，实际推送最后由用户确认。版本保持 0.1.0，补充版本变更和交付记录，完整本地交付测试 179 项通过。目标为用户新建的公开仓库 `Resker666/LumaBox`，已确认空仓库、默认分支 main，配置了本地 origin；整理完成后建立本地 main，保留原特性分支，实际推送和发布待用户确认。
