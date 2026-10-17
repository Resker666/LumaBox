# Windows 下载结果编码修复

日期：2026-10-17。修复本地 0.6.0 验收中，下载后误报“文件完整性检查未通过”的问题。

## 根因与修改

真实作品 `7676043714435897019` 的命令行进程已成功下载并通过完整解码，文件大小及 SHA-256 正确，但 Windows 重定向 stdout 使用 GBK。中文标题在 stdout 中是 GBK 字节，网页 worker 读取 JSON 字节时按 UTF-8 解码失败，异常被归为 `VERIFY_FAILED`。同一份 stdout 按 GBK 解码后与磁盘 UTF-8 报告完全一致。

- CLI 的 JSON 使用 ASCII 转义传输，也属于合法 UTF-8；反序列化后中文、表情及路径保持原值，不依赖 Windows 终端编码或启动器环境变量。
- 无效 JSON、非 JSON 对象、空结果、过深嵌套和超过 256 KiB 的输出使用 `RESULT_PARSE_FAILED`，网页提示“下载结果读取失败，请重试；此错误不代表媒体文件损坏。”
- 媒体完整解码、报告一致性、文件大小、SHA-256、路径边界及发布条件保持不变；实际校验失败继续使用 `VERIFY_FAILED`。结果解析失败不会触发自动网络重试，失败清理后队列继续处理。

## 回归证据

新增 13 项回归，先在旧实现观察到全部失败；纯中文 / GBK 用例明确复现 `UnicodeDecodeError`，含表情及 ASCII stdout 的用例复现 `UnicodeEncodeError`。修复后通过。

覆盖真实 CLI 子进程的 GBK / ASCII stdout、中文和表情、中文路径、CLI → worker → 视频发布 → 可选音频，以及六类无效结果的清理、错误区分与后续队列。Chromium、WebKit 在 390×844 手机尺寸验证两类提示不同且没有横向溢出。另补充新错误不自动重试的策略检查。

针对性运行：`tests/test_result_encoding.py`、`test_cli.py`、`test_auto_retry.py`、`test_retry_policy.py`、`test_web_jobs.py`，结果 **88 passed, 1 skipped**。

首次完整回归：**581 passed, 1 failed, 1 skipped, 1 warning，435.36 秒**。唯一失败是既有 `tests/test_retry_ui.py::test_late_duplicate_view_handles_current_target_state[hidden_before-webkit]`，等待“恢复并查看”按钮 30 秒超时。随后未修改源码或测试，单独重跑整个 `tests/test_retry_ui.py` 模块，**12 passed，29.58 秒**，包含上述用例。因此本次没有将首次全量运行记为全绿。跳过项为 Windows 符号链接权限条件；警告为已有 Starlette TestClient / httpx 弃用提示。

完整回归与复测的原始证据位于忽略目录 `Downloaded/result-encoding-validation/full-suite.log`、`full-suite.xml`、`retry-ui.xml`。

独立只读审查未发现本次修复的阻塞问题。

## 真实下载

重启本地 `http://127.0.0.1:8766/` 验收服务，保留原数据与失败记录。通过网页 API 提交同一作品的直链并开启“同时保存音频”，完成真实浏览器下载、视频发布及音频生成：

| 文件 | 字节数 | 时长（秒） | SHA-256 |
|---|---:|---:|---|
| MP4 | 2,486,489 | 18.733333 | `b04df3c35fe12f4ba171fbcaae9b8b362b143f0a15f125ab6e9a90d10585efa2` |
| M4A | 304,182 | 18.690998 | `2c0898f409290ab96f3d5f68063b540316b1a70b70b285251f2dfd9951dc6190` |

网页状态与音频状态均为 `success`，中文标题正确。两份文件经 HTTP 下载后的字节数和 SHA-256 与存储索引一致。私有验证结果位于忽略目录 `Downloaded/result-encoding-validation/real-canonical.json`。

此前第一次短链重试停在 `TARGET_UNCONFIRMED`，没有进入媒体传输；它属于页面身份无法确认，区别于本次修复的结果编码错误。随后同一作品的 `/video/7676043714435897019` 直链完成上述验证，没有调整访问或文件检查规则。

本地 0.6.0 wheel 已重新构建，41 个运行文件与源码逐字节相同，SHA-256 为 `5cae2190632bc9a0e0850aad074cf09bc22af0a1bf607bc9aa14016b161d62b1`。本轮没有推送 GitHub 或升级云服务器。
