# 图集原始 MP3 接入计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 图集在用户显式选择时独立保存对应的原始 MP3，图片成功结果保持可用。

**Architecture:** 沿用现有串行音频附件队列。视频从本地 MP4 提取 M4A；图集在受控浏览器中重新确认目标作品背景播放器，并下载原始 MP3。父进程拥有输出文件句柄和文件归属，子进程负责短期浏览器会话；验证完成后才建立公开文件索引。

**Tech Stack:** Python 3.11+、Playwright、FFmpeg/FFprobe、FastAPI、现有原生 JavaScript 页面。

**Spec:** 用户于 2026-10-18 选择“图集配乐”和“原始 MP3”；对应可行性证据见 `docs/validation/2026-10-18-gallery-music-p0.md`。

## Global Constraints

- 默认不保存音频；支持已有收藏补保存。MP3 原始字节不转码，视频 M4A 规则不变。
- 不持久化签名媒体 URL、Cookie；不扩大视频和图片下载域名范围，不放宽公网 DNS/TLS 检查。
- 配乐失败、取消、重启恢复均独立于图片结果，保留文件归属、租约和空间检查。
- 本次仅本地开发验证；云端部署、GitHub 推送等待用户另行指令。

## Review Focus

- 背景音乐与动态照片视频、推荐视频混淆：仅接受已确认作品内、轮播页外的唯一背景播放器。
- 下载中取消与浏览器子进程遗留：进程树回收、受控输出句柄、取消回归测试。
- MP3 丢失后图片收藏被误判不可用：可选附件不影响重复收藏识别，可单独补保存。
- 错误媒体和短文件：严格 MP3 编码、完整长度、播放器时长及全量解码检查。
- 旧文件清理和迁移：沿用 inode 清单，保留未知替代文件和旧图片，不扩大删除范围。

### Task 1: MP3 核心

**Files:** 新增 `src/lumabox/gallery_music.py`、`tests/test_gallery_music.py`；必要时扩展 `browser.py`、`extract.py`、`urls.py`。根任务负责 `web/process.py` 的可选 stderr 参数。

**Interfaces:** `download_gallery_music(work_id, target, *, headless=False, cancelled, max_bytes, on_created) -> dict`，返回 `bytes/sha256/duration/mime`，`mime=audio/mpeg`。创建文件后立即记录 device/inode；任何错误仅清理自己创建的文件。

- [x] 先写目标作品、背景播放器唯一性、下载限制、MP3 编码与取消测试并观察失败。
- [x] 实现短期受控浏览器、严格音频主机校验、原始字节传输和本地完整解码验证。
- [x] 运行 `python -m pytest tests/test_gallery_music.py -q`，44 项通过。

### Task 2: 附件管理

**Files:** `web/audio.py`、`web/store.py`、`web/duplicates.py`、`web/media.py`、`web/storage.py`、`web/app.py`；新增 `tests/test_gallery_audio_jobs.py`。

**Interfaces:** 沿用 `/api/jobs/{id}/audio` 与取消接口；图集索引 `audio/mpeg/.mp3`，视频索引 `audio/mp4/.m4a`。已保存图片作为收藏租约锚点，不重新下载图片。

- [x] 新增选中后排队、已有图集补保存、MP3 Range/下载、失败/取消保留图片和清理测试，观察 9 项失败。
- [x] 完成图集附件分支、MIME/文件清理支持，并运行新测试与原视频音频测试，37 项通过。
- [x] 验证缺失配乐、恢复、存储删除和批量去重行为。

### Task 3: 手机页面

**Files:** `web/static/app.js`、`web/static/index.html`、`tests/test_mobile_audio_ui.py`。

- [x] 先写默认关闭、图集保存配乐、MP3 试听/下载和失败保留图片的浏览器断言，观察失败。
- [x] 接通原有选项与更多菜单，兼容两种音频 MIME，维持紧凑手机布局。
- [x] 运行相关真实浏览器 UI 测试并核对截图，原 19 项与最终新增/调整后的 5 项图集定向检查通过。

### Task 4: 本地集成验收

- [x] 用用户提供的图集运行正式功能，核对 12 张图片和原 MP3 的长度、哈希、时长及解码。
- [x] 验证浏览器 MP3 播放、Range、下载与图片 ZIP 内容不变。
- [x] 执行完整 `python -m pytest -q`，709 项通过、1 项跳过；记录结果和实际限制，更新版本、说明与待办。
- [x] 保留本地可体验服务（8766），原 9 条历史、9 个索引与 13 个输出文件/哈希保持；不部署、不推送。

### Task 5: 用户本地验收后的 CDN 与诊断修复

用户于 2026-10-18 反馈配乐地址被拒绝，分析后授权修复，沿用当前工作目录和 0.8.0 范围。

- [x] 先实测 lf9 直接资源及正常重定向到 v9 分发节点，确认原始 MP3 字节、播放器时长与完整解码。
- [x] 新增预期失败的域名兼容、无效地址、重定向和诊断丢失回归，扩展精确音乐来源与仅重定向可用的目标。
- [x] 增加兼容旧数据库的脱敏配乐诊断持久化，重试、成功与恢复状态清理旧原因。
- [x] 页面区分域名、非公网 DNS、解析、连接和跳转错误；11 项图集/诊断浏览器检查通过。
- [x] 正式网页补保存原失败任务的配乐；原 10 条任务、22 个索引、27 个输出文件/哈希保持，12 张图片与 ZIP 未重下或改写。
- [x] 独立审查无待修复问题；最终完整测试 728 项通过、1 项跳过。见 `docs/validation/2026-10-18-gallery-music-cdn-fix.md`。
