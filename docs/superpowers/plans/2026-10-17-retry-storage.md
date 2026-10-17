# LumaBox 0.5 Retry and Storage Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按 A → B 交付有限自动重试、已有作品复用，以及能够管理隐藏收藏的空间展示和单作品文件删除。

**Architecture:** 沿用 FastAPI、SQLite、单个 worker 和独立 CLI 子进程。重试与判重扩展现有任务状态，存储统计与删除通过独立 StorageManager 管理；所有媒体仍经过现有完整解码及发布校验。先完成并验证 A，再实现 B。

**Tech Stack:** 保留项目 Python 3.11+ 下限（现有云端为 3.12）、sqlite3、FastAPI/Starlette、Playwright Chromium/WebKit、原生 HTML/CSS/JavaScript、pytest；不增加运行依赖。

**Spec:** [已确认的设计](../specs/2026-10-17-retry-storage-design.md)。状态：计划待用户审阅及选择执行方式。

## Global Constraints

- 首次失败等待 **10 秒**，最多自动重试 **1 次**；每个逻辑任务最多两次尝试，仍在同一任务卡。
- 两次尝试及等待共享 **60 分钟总期限**；一次一个处理位置，最多 10 个排队任务，等待重试占用当前处理位置。
- 自动重试保持原视频上限，使用新匿名浏览器和干净目录；手动重试按当前表单上限创建新任务。
- 仅重试结构化的连接重置、连接超时、DNS 超时及资源等待超时；禁止条件始终优先。
- 两次尝试之间允许进度归零；最多保存两条脱敏尝试结果，不能记录原始异常、Cookie 或签名地址。
- 本地和云端、取消、重启恢复、隐藏历史、既有媒体验证和云端访问保护都要继续可用。
- 文件统计缓存最多 **30 秒**，包括原图、动态片段、ZIP、隐藏收藏及其他/临时内容；不随任务轮询扫描目录。
- 默认长期保留；只增加用户确认后的单作品删除。活动任务、文件租约、越界、链接和无法确认归属的文件阻止删除。
- 不修改用户现有收藏进行删除验收；不升级线上或推送源码，直到结果可审阅且用户另行指示。
- 当前工作目录为原仓库，分支 `feat/retry-storage`，基于 `8cf9c19`；前一版 PR #4 尚待合并。保留原目录工作方式，避免把本版代码混入 PR #4。

## Review Focus

1. 同一作品用不同短链提交，子进程在最后一次进度后立即退出：发布前仍须判重。由 Task 3 的 `test_duplicate_after_last_progress_is_not_published` 验证。
2. 已隐藏的旧收藏仍有文件，但部分文件被手动移走：不允许假复用或假恢复。由 Task 3 的 `test_hidden_missing_media_does_not_block_new_download` 验证。
3. 用户取消恰好发生在等待重试、判重或发布前：取消不能被重试或引用旧结果覆盖。由 Task 2、3 的状态竞争测试验证。
4. 删除后进程退出或切换 output 目录再启动：保留用户已确认意图，只处理原定且仍属于当前根目录的文件。由 Task 6 的中断和换目录测试验证。
5. HTTP 连接在开始传输前断开：所有下载/预览租约均须释放，否则删除会一直被拒绝。由 Task 6 的真实响应发送异常测试验证。

## File Structure

- `src/lumabox/web/retry_policy.py`：重试常量及纯策略判定。
- `src/lumabox/web/duplicates.py`：重复匹配结果、冲突响应元数据和可用性检查；不自行创建数据库连接。
- `src/lumabox/web/store.py`：增量迁移、原子任务状态转换、重试预算、重复匹配事务及隐藏记录恢复。
- `src/lumabox/web/worker.py`：单次尝试、受期限约束的重试等待、取消/关闭及发布前判重。
- `src/lumabox/web/storage.py`：受控目录统计、30 秒缓存、分页管理及文件删除生命周期。
- `src/lumabox/web/app.py`、`media.py`：有界 API 接口、结构化冲突和一致的文件租约释放。
- `src/lumabox/web/static/app.js`、`index.html`、`app.css`：A 的交互；新增 `storage.js` 承载 B 的面板和确认操作。
- `tests/conftest.py` 增加共享 `web_config(tmp_path)` 夹具，返回 `WebConfig(tmp_path / 'data')`；现有测试自己的配置夹具不改语义。`tests/test_retry_policy.py`、`test_auto_retry.py`、`test_duplicates.py`、`test_retry_ui.py`、`test_storage.py`、`test_storage_ui.py` 按下列任务验证新行为；受控子进程辅助代码放测试工具，不进入生产类。

## Task 1: Retry policy and durable task transitions

**Files:** Create `src/lumabox/web/retry_policy.py`, `tests/test_retry_policy.py`; modify `src/lumabox/web/store.py`, `tests/conftest.py`; create store cases in `tests/test_auto_retry.py`.

**Interfaces:**
- `retry_policy.RETRY_DELAY_SECONDS = 10`, `MAX_ATTEMPTS = 2`。
- `should_retry(error: str, diagnostics: dict) -> bool`：只判定故障类别，不决定任务预算。
- `JobStore.schedule_retry(job_id: str, error: str, result: dict, *, now: float | None = None) -> bool`：校验状态、预算和截止时间，原子进入 `retry_wait`。
- `JobStore.begin_retry(job_id: str, *, now: float | None = None) -> dict | None`：仅已到时间且仍等待的任务进入第二次运行。
- 保存 `attempt_count`、`retry_at`、`deadline_at`、有界 `attempts`；沿用 `started_at` 为整个逻辑任务起点。`claim()` 返回首次 `running` 或待恢复的唯一 `retry_wait` 任务，后者不提前消费第二次次数。

- [x] **Step 1: Write failing policy and store tests.** 断言连接重置、连接超时、DNS 超时及 `RESOURCE_TIMEOUT` 被允许；每一种禁止错误即使附带可重试网络原因也被拒绝。未知 `DOWNLOAD_FAILED` 返回 false。

```python
def test_nonpublic_dns_overrides_timeout():
    assert not should_retry("RESOURCE_TIMEOUT", {"network_reason": "DNS_NON_PUBLIC"})

def test_resource_timeout_is_retryable():
    assert should_retry("RESOURCE_TIMEOUT", {})
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_retry_policy.py tests/test_auto_retry.py -q`；确认失败是缺少策略/状态，而非测试环境错误。
- [x] **Step 3: Implement the policy and migrations.** 精确使用 10 秒及两次尝试；旧终态不自动重新执行。保存的尝试结果使用 `public_diagnostics`，数量最多两条。新增状态同步到 get/search/claim/cancel/remove/recover/expire，重试等待算当前处理任务；运行时中断仍终止，等待状态重启保留时间和预算。
- [x] **Step 4: Verify state tests.** 断言 `retry_at == now + 10`、初始 `deadline_at == started_at + config.task_timeout`；第二次失败不再调度；取消后 `begin_retry()` 返回 None；截止时间不重置；重开 SQLite 后次数和等待时间一致；新尝试进度可归零。Run 上述测试及 `tests/test_cancel_diagnostics.py`。
- [x] **Step 5: Commit.** `feat: add bounded retry policy and persistent attempt state`。

## Task 2: Retry execution in the real worker

**Files:** Modify `src/lumabox/web/worker.py`; extend `tests/test_auto_retry.py`; add controlled subprocess helper in `tests/web_helpers.py`.

**Interfaces:**
- Consumes Task 1 的策略、调度和开始方法。
- `JobWorker._run_attempt(job: dict) -> None`：只运行一次尝试；沿用 ProcessTree 和发布校验。失败将已解析的最终报告交给调度层，不能提前把可重试任务变成终态。
- `JobWorker._wait_retry(job: dict) -> dict | None`：用 `stopping.wait()` 分段等待，定期检查取消及逻辑任务截止时间，关闭时保留尚未运行的等待状态。
- `run_once() -> bool` 的含义保持不变：有任务被处理则 true，无任务则 false。
- 测试辅助接口 `retry_worker(config, store, tmp_path, fixture, *, failures: int = 1)`：返回真实子进程 FixtureWorker；前 failures 次输出连接重置，后续成功，启动计数放 `tmp_path / 'attempt-count.txt'`。

- [x] **Step 1: Write failing integration tests.** 辅助子进程把启动次数记到任务目录之外的测试私有文件；第一次返回结构化连接重置、第二次输出既有真实媒体夹具的合法报告。断言同一 job ID、两次子进程、第二次目录没有第一次残留、最终文件哈希一致。

```python
def test_real_child_retries_in_same_job(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    job = store.create(URL)
    worker = retry_worker(web_config, store, tmp_path, media_fixtures[0])
    assert worker.run_once()
    assert store.get(job["id"])["status"] == "success"
    assert store.get(job["id"])["attempt_count"] == 2
    assert (tmp_path / "attempt-count.txt").read_text() == "2"
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_auto_retry.py -q`。
- [x] **Step 3: Implement attempt orchestration.** 每次启动前检查剩余总预算、空间和取消，清理成功才调度重试；重置本次进度快照。等待期间保持串行，不额外领取任务。正常子进程结束后也先检查取消，再发布；停止服务不能触发一次新的重试。
- [x] **Step 4: Verify failure and race cases.** 两次失败最多两次启动；等待时取消不再启动；等待时 stop 可及时返回；关闭运行进程标为中断；清理失败不重试；第二次尝试受剩余期限限制。至少一个测试使用真实 10 秒等待；其余通过受控时间推进测试状态，无需修改产品配置增加仅供测试的参数。Run `tests/test_auto_retry.py tests/test_web_jobs.py tests/test_daily_worker.py tests/test_cancel_diagnostics.py`。
- [x] **Step 5: Commit.** `feat: retry transient failures once within the task deadline`。

## Task 3: Transactional duplicate handling and hidden-result restore

**Files:** Create `src/lumabox/web/duplicates.py`, `tests/test_duplicates.py`; modify `store.py`, `worker.py`, `app.py`.

**Interfaces:**
- `find_match(store, db, *, url: str, work_id: str | None, kind: str | None, exclude_id: str | None = None, force_download: bool = False) -> dict | None`：在调用者现有事务内查询；返回固定元数据，含 `id`, `match_type`, `hidden`, `work_id`, `title`。
- `DuplicateWorkError(WebError)`：固定冲突码 `ALREADY_DOWNLOADING` 或 `DUPLICATE_WORK`，在 `existing: dict` 中携带上面的有限匹配信息。
- `JobStore.create(..., force_download: bool = False)`：严格布尔值；同一事务内先判重再检查排队容量，避免队列满时仍无法查看已有结果。
- `JobStore.check_duplicate(job_id: str, work_id: str, kind: str) -> dict | None` 与 `mark_duplicate(job_id: str, target_id: str) -> bool`：再次核对匹配和当前状态，取消优先；存 `duplicate_of`、`force_download`。
- `JobStore.restore(job_id: str) -> dict`：仅恢复文件仍可用且未处于删除生命周期的隐藏成功收藏。此任务即增加 `storage_state TEXT NOT NULL DEFAULT 'present'` 兼容列，Task 6 再补充删除意图字段。
- `POST /api/jobs/{job_id}/restore`：空 JSON body；原有 CSRF/认证生效。

- [x] **Step 1: Write failing tests.** 相同规范链接的重复点击及并发提交只保留一个活动任务；成功结果提示复用；force 仅跳过成功结果；隐藏可用结果可恢复，隐藏但缺失媒体不阻止新下载；完整链接与已解析短链按 ID 匹配。

```python
def test_active_work_cannot_be_forced_twice(web_config):
    store = JobStore(web_config)
    first = store.create(URL)
    with pytest.raises(DuplicateWorkError) as failure:
        store.create(URL, force_download=True)
    assert failure.value.code == "ALREADY_DOWNLOADING"
    assert failure.value.existing["id"] == first["id"]
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_duplicates.py -q`。
- [x] **Step 3: Implement matches and lifecycle.** 可用性检查遵守当前输出根目录、链接拒绝、大小及可读性；避免完整哈希全历史扫描。worker 首次可信身份和发布前各检查一次，停止新子进程后清理本次产物，再标记 `duplicate`；不能引用标题相同但 ID 不同的作品。发布前的兜底检查在现有 `_verify` 确认最终报告身份后执行，不根据未验证报告引用已有结果。
- [x] **Step 4: Verify late and concurrent cases.** 增加 `test_duplicate_after_last_progress_is_not_published`、`test_hidden_missing_media_does_not_block_new_download`、取消与判重竞争、原引用结果被移除/删除的处理。修改旧队列测试：需要不同任务时使用不同作品链接；需要主动再下成功作品时显式 force，不把旧的“相同 URL 创建十次”行为留成期望。Run `tests/test_duplicates.py tests/test_web_jobs.py tests/test_daily_api.py tests/test_web_api.py`。
- [x] **Step 5: Commit.** `feat: reuse existing works and prevent duplicate active downloads`。

## Task 4: A user interface and browser acceptance

**Files:** Modify `static/app.js`, `index.html`, `app.css`; create `tests/test_retry_ui.py`; adjust relevant existing UI fixtures to use force or distinct links when the test intentionally creates several tasks.

**Interfaces:** API 冲突保留 `error`, `message`, `existing`，前端 api helper 的错误对象携带固定字段。任务响应增加 `attempt_count`, `retry_at`, `attempts`, `duplicate_of`；公开状态新增 `retry_wait`, `duplicate`。

测试夹具 `retry_server` 返回 `(url, worker)`，使用 Task 2 的 retry_worker 和现有真实 HTTP 测试服务启动方式；所有媒体均由测试生成。

- [x] **Step 1: Write failing browser tests.** 手机宽度显示等待重试倒计时和第 2 / 2 次尝试；等待时可取消；重复提示能查看/恢复旧结果，主动重下需确认额外空间；判重终态链接到旧卡片，目标不在当前页时仍能查询并定位。

```python
def test_retry_wait_can_cancel(retry_server, browser):
    url, worker = retry_server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url)
        submit(page, URL)
        expect(page.get_by_text("等待重试", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="取消下载", exact=True).click()
        expect(page.get_by_text("已取消", exact=True)).to_be_visible()
    finally:
        page.close()
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_retry_ui.py -q`。
- [x] **Step 3: Implement A UI.** 复用现有米白/橙/绿样式和已有任务卡，不把两次尝试显示成两条收藏。同步状态筛选、队列说明、取消入口、失效引用和脱敏尝试记录；轮询不能重置用户已展开的预览或确认选择。
- [x] **Step 4: Verify A.** Chromium 与 WebKit 运行新测试及取消/登录回归；同一测试上下文不能用 Chromium 手机尺寸代替 WebKit。Run `tests/test_retry_ui.py tests/test_cancel_ui.py tests/test_cloud_login.py tests/test_daily_ui.py tests/test_live_note_ui.py`，然后运行一次完整 `pytest -q`，通过后进入 B。
- [x] **Step 5: Commit.** `feat: show retry attempts and existing-work actions in the web UI`。

## Task 5: Storage inventory, caching and management reads

**Files:** Create `src/lumabox/web/storage.py`, `tests/test_storage.py`; modify `store.py`, `app.py`。

**Interfaces:**
- `StorageManager(store)` 由 `JobStore` 在迁移完成后构造并保存为 `store.storage`，共用 store 的锁；使用类型检查引用避免循环导入。
- `summary(*, refresh: bool = False) -> dict`：`output_bytes`, `saved_bytes`, `hidden_bytes`, `other_bytes`, `saved_count`, `disk_free_bytes`, `sampled_at`, `complete`。
- `items(*, scope: str = 'all', page: int = 1, page_size: int = 20) -> dict`：默认有文件或删除待处理的已知任务，scope 可为 `all` 或 `hidden`。返回 jobs 的有限元数据、文件计数、当前字节、hidden 和 storage_state；不提供客户端路径选择。
- `invalidate() -> None`：令缓存失效；30 秒 TTL，任务发布、移除、恢复、自动清理及删除后调用。
- `GET /api/storage?refresh=1`、`GET /api/storage/items?scope=...&page=...`：严格参数及分页边界，登录保护沿用现有入口。

- [x] **Step 1: Write failing inventory tests.** 已生成视频、带动态片段图集、ZIP、隐藏收藏、未知文件与临时文件分别计数；文件丢失、权限失败和目录中的链接不被冒充为完整统计。历史大小与当前大小分别断言。

```python
def test_unknown_file_is_counted_without_becoming_a_collection(web_config):
    store = JobStore(web_config)
    (web_config.output_dir / "user-file.bin").write_bytes(b"abc")
    summary = store.storage.summary(refresh=True)
    assert summary["output_bytes"] == summary["other_bytes"] == 3
    assert summary["saved_count"] == 0
    assert store.storage.items()["total"] == 0
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_storage.py -q`。
- [x] **Step 3: Implement scan and cache.** 不跟随 symlink/junction，扫描异常标记不完整；同一扫描结果用于概览和管理项。`hidden_bytes` 是 `saved_bytes` 的子集，不能再次相加；生成报告和不能对应正式媒体的内容进入 `other_bytes`。仅当前 output 计入合计，其他目录仅通过系统磁盘可用量间接反映。分页参数先校验，缓存锁避免多个轮询同时扫描；文件操作后统一失效。
- [x] **Step 4: Verify reads and boundaries.** 测试 30 秒以内复用、过期或手动刷新重新扫描、变更后立即失效、隐藏收藏列表及恢复；未登录、跨站/畸形写入不能访问或触发副作用。Run `tests/test_storage.py tests/test_web_api.py tests/test_cloud_login.py`。
- [x] **Step 5: Commit.** `feat: report current storage usage and retained hidden collections`。

## Task 6: Confirmed purge with leases and restart recovery

**Files:** Extend `storage.py`, `store.py`, `app.py`, `media.py`; extend `tests/test_storage.py` and streaming cases in `tests/test_daily_api.py`。

**Interfaces:**
- `StorageManager.purge(job_id: str) -> dict`：仅由显式确认的 API 或此前持久化删除意图调用；返回 `status`, `deleted_bytes` 和固定原因。删除未完成不能返回成功。
- `recover_deletions() -> None`：启动时核对根目录和归属，只恢复已确认意图。
- `storage_state` 为 `present`, `deleting`, `delete_failed`, `deleted`；保存删除时的输出根目录及受控文件清单，部分删除后不能丢失索引。
- `POST /api/jobs/{job_id}/purge` body 必须精确为 `{"confirm": true}`；活动任务返回 `JOB_ACTIVE`，有租约返回 `FILE_IN_USE`，不确认的请求不触碰文件。
- 所有 file/open_file/restore/duplicate 读取拒绝 `deleting`, `delete_failed`, `deleted`；文件迁移不得再复制这些记录。

- [x] **Step 1: Write failing deletion tests.** 只删除新生成的临时作品；确认前不变；租约占用、活动/取消/等待重试任务不可删；隐藏作品可删；重复删除幂等；隔壁作品和输出目录外样本完整保留。

```python
def test_pending_job_cannot_be_purged(web_config):
    store = JobStore(web_config)
    job = store.create(URL)
    with pytest.raises(WebError, match="JOB_ACTIVE"):
        store.storage.purge(job["id"])
    assert store.get(job["id"])["status"] == "pending"
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_storage.py -q`。
- [x] **Step 3: Implement deletion intent and verified cleanup.** 同一锁下验证任务、归属和零租约，持久化意图并阻止新读取。只删除已知文件清单及生成的报告，发现额外未知内容、链接或越界即拒绝。每次实际文件操作前重新核对绝对路径仍在预期根内；不直接复用会删除未知子文件的通用 cleanup。文件缺失允许完成；权限失败保留状态和剩余归属信息。
- [x] **Step 4: Integrate recovery and download leases.** 启动时在输出迁移/过期清理前识别删除意图；变更 output 时不能对新目录套用旧意图，保留待处理说明。旧的 recover/expire/cleanup 路径必须跳过非 `present` 生命周期，不能绕过删除清单或重新复制已删除结果。下载也使用现有 `LeasedResponse` 的 finally 释放方式及线程安全的一次性 release，覆盖传输前断开/发送异常；GET/HEAD/Range 预览行为保持一致。
- [x] **Step 5: Verify hostile and interrupted cases.** 路径穿越、symlink、Windows junction、未知额外文件、并发请求、删除失败后重启、目录原本不存在、换 output 重启都不越界。真实响应测试验证普通下载和 Range 持租约时拒删、断开后可删，并覆盖发送开始前异常。Run `tests/test_storage.py tests/test_daily_api.py tests/test_web_jobs.py tests/test_cancel_diagnostics.py`。
- [x] **Step 6: Commit.** `feat: safely purge confirmed collection files with durable recovery`。

## Task 7: B storage panel and explicit removal choices

**Files:** Create `static/storage.js`, `tests/test_storage_ui.py`; modify `static/index.html`, `app.css`, `app.js`。

**Interfaces:** `window.LumaBoxStorage.init({request, formatSize, onChanged})` 创建面板控制器，返回 `{refresh(force = false), invalidate()}`；app 在取得会话后注入现有 api helper 和 size formatter，onChanged 用于刷新历史，不另建认证逻辑。脚本使用 defer 并保持加载顺序。

- [x] **Step 1: Write failing browser tests.** 手机/桌面概览、统计更新时间、手动刷新、分页与隐藏收藏；“仅移除记录”后文件仍在且可恢复；取消彻底删除确认不发送请求；确认框显示标题/ID、文件数量、大小和不可恢复提示。

```python
def test_storage_panel_fits_phone(daily_server, browser):
    url, worker = daily_server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url)
        page.get_by_role("button", name="文件管理", exact=True).click()
        expect(page.get_by_text("系统可用空间", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()
```

- [x] **Step 2: Verify RED.** Run `.\.venv\Scripts\python.exe -m pytest tests/test_storage_ui.py -q`。
- [x] **Step 3: Implement B UI.** 概览放原侧栏，详细管理独立区域；显示系统可用量与收藏大小的不同含义、统计不完整及其他/临时内容说明。有租约时提示停止预览/等待下载；删除失败保留重试入口，不假装已经释放空间。刷新任务卡不重建已打开管理对话框。
- [x] **Step 4: Verify both engines.** Chromium 和 WebKit 覆盖查看隐藏文件、恢复、取消确认、确认删除、租约拒绝与失败重试。无横向溢出，键盘焦点可到确认/取消按钮，成功后历史和统计更新。Run `tests/test_storage_ui.py tests/test_retry_ui.py tests/test_daily_ui.py tests/test_cloud_ui.py tests/test_cloud_login.py`。
- [x] **Step 5: Commit.** `feat: add storage management and explicit file deletion controls`。

## Task 8: Joint verification and reviewable delivery

**Files:** Modify `pyproject.toml`, `src/lumabox/__init__.py`, `README.md`, `CHANGELOG.md`, `TODO.md`；create `docs/validation/2026-10-17-retry-storage.md`（若实际验收跨日，文件日期随实际发生日期），必要时更新 deployment 文档的升级/回退说明。

- [x] **Step 1: Run full regression.** `.\.venv\Scripts\python.exe -m pytest -q`；要求零失败，列出跳过和警告的具体原因。源代码变化后针对性复测，避免无变化时反复跑完整套件。
- [x] **Step 2: Review requirements and branch.** 对照 spec 每条验证标准记录证据；按选择的执行流程完成独立审查，修复重要问题后复测受影响项。`git diff --check` 通过；检查没有把凭据、数据库、下载文件、venv 或验证工件加入 Git。
- [x] **Step 3: Prepare real acceptance without touching production data.** 从已有私有配置读取凭据，不打印；在云端独立临时数据/输出目录运行候选代码，按个人使用授权样本分别验证普通视频、12 张图集、单页动态照片的完整性和 ZIP；临时故障用受控测试服务。删除验收只针对新生成样本，保留线上收藏。
- [x] **Step 4: Record actual outcomes and version.** 实际通过后更新 0.5.0 版本及验证记录，区分离线测试、平台实测、用户实机、未验证项；不能把平台拒绝或网络失败改写成成功。保留 VPN 低优先级待办与 HTTPS 备案依赖。
- [x] **Step 5: Commit and report.** `docs: record 0.5 retry and storage validation`。交付测试结果和可体验入口；线上升级与 GitHub 推送依用户后续指示进行，前一版 PR #4 的合并状态要在发新 PR 前重新确认。

## Execution Handoff

推荐 **Native**：在当前会话按 Task 1 → 8 实现，最后做一次独立审查。A 的状态迁移、判重与 B 的删除生命周期共享同一 SQLite 和文件租约，顺序实施能减少接口交接。

另一种选择为 **Subagent-driven**：每个任务交给独立子代理实现并独立审查，最后再做整分支审查；上下文和审查次数更多。待用户审阅本文件并选择后执行。
