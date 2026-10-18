"use strict";
const $ = (id) => document.getElementById(id);
const labels = { pending: "待下载", running: "下载中", retry_wait: "等待重试", duplicate: "已有收藏", cancelling: "正在取消", cancelled: "已取消", success: "已完成", failed: "未完成", expired: "已过期" };
const stages = { browser_start: "启动浏览器", browser_load: "读取作品", media_transfer: "下载媒体", media_verification: "检查文件", publication: "整理收藏" };
const failures = {
  CLEANUP_FAILED: "临时文件未能清理，已停止自动重试，请检查保存位置。",
  AUTH_REQUIRED: "此作品需要登录后访问，暂时无法下载。",
  CHALLENGE_REQUIRED: "页面要求访问验证，暂时无法下载。",
  UNSAFE_TARGET: "网络地址校验未通过，请检查网络后重试。",
  BLOCKED: "平台暂时拒绝访问，请稍后重试。",
  RATE_LIMITED: "平台请求过于频繁，请稍后重试。",
  NOT_FOUND: "作品已不可访问，请检查链接。",
  RESOURCE_TIMEOUT: "页面或媒体加载超时，请检查网络后重试。",
  TIMEOUT: "任务超过等待时间，请稍后重试。",
  UNSUPPORTED_MEDIA: "暂时无法确认这条作品的完整媒体。",
  PARSE_CHANGED: "作品页面发生变化，暂时无法读取。",
  RESULT_PARSE_FAILED: "下载结果读取失败，请重试；此错误不代表媒体文件损坏。",
  VERIFY_FAILED: "文件完整性检查未通过，请重新下载。",
  STORAGE_FULL: "磁盘空间不足，请释放空间或调低视频上限。",
  DEPENDENCY_MISSING: "下载组件尚未安装完整，请检查安装说明。",
  INTERRUPTED: "任务已中断，可以重新提交。",
  DOWNLOAD_FAILED: "下载未完成，请检查网络后重试。"
};
const networkFailures = {
  DNS_NON_PUBLIC: "DNS 返回虚拟或非公网地址，当前地址检查已停止连接。请检查 DNS 或代理设置后重试。",
  DNS_LOOKUP_FAILED: "DNS 解析失败，无法取得网站地址。请检查网络和 DNS 设置后重试。",
  DNS_TIMEOUT: "DNS 解析超时，请检查网络和 DNS 设置后重试。",
  CONNECTION_REFUSED: "连接被拒绝，请检查网络或代理设置后重试。",
  CONNECTION_RESET: "连接被重置，下载连接已中断，请稍后重试。",
  CONNECTION_TIMEOUT: "连接超时，未能及时取得响应，请检查网络后重试。",
  CONNECTION_UNREACHABLE: "目标网络不可达，请检查网络或代理路由后重试。",
  TLS_ERROR: "TLS 安全连接失败，请检查系统时间、证书或代理设置后重试。",
  CONNECTION_FAILED: "网络连接失败，请检查网络或代理设置后重试。"
};
function audioDiagnosticText(detail) {
  if (!detail) return null;
  if (networkFailures[detail.network_reason]) return networkFailures[detail.network_reason];
  if (detail.policy_reason === "AUDIO_HOST_UNSUPPORTED") {
    const host = typeof detail.host === "string" && /^[a-z0-9.-]{1,253}$/i.test(detail.host) ? detail.host : null;
    return host ? `暂不支持此配乐域名：${host}。` : "暂不支持此配乐域名，请稍后重试。";
  }
  const policies = {
    AUDIO_URL_INVALID: "配乐地址格式无效，无法保存，请稍后重试。",
    AUDIO_REDIRECT_LIMIT: "配乐地址跳转次数过多，请稍后重试。"
  };
  return policies[detail.policy_reason] || null;
}
const mediaFailures = {
  TARGET_UNCONFIRMED: "页面已打开，但尚未确认目标作品区域，暂未保存文件。",
  WORK_CHANGED: "页面切换到了其他作品，下载已停止，请重新提交原链接。",
  MEDIA_COUNT_UNCONFIRMED: "已识别作品页面，但无法确认完整页数，暂未保存文件。",
  MEDIA_ORDER_UNCONFIRMED: "作品图片尚未全部加载，或无法确认完整顺序，暂未保存文件。",
  MEDIA_URL_UNAVAILABLE: "播放器尚未提供可下载的完整媒体地址，请稍后重试。",
  MOTION_URL_UNAVAILABLE: "已识别动态照片，但动态片段尚未加载完整，请稍后重试。",
  IMAGES_LOADING: "作品仍在显示默认占位图片，请稍后重试。"
};
const history = { q: "", status: "all", page: 1, page_size: 20, scope: "history" };
const activeAudio = new Set(["waiting", "pending", "running", "cancelling"]);
const isAudioFile = (file) => ["audio/mp4", "audio/mpeg"].includes(file.mime);
const audioMessages = {
  waiting: "视频完成后将自动提取音频。", pending: "音频已排队，稍后开始。",
  running: "正在提取音频…", cancelling: "正在取消音频…", cancelled: "音频已取消，视频已保留。",
  success: "音频已保存 · M4A", unsupported: "图集暂不提取音频，图片已保留。"
};
const audioFailures = {
  NO_AUDIO: "此视频没有音轨，视频已保留。", SIZE_LIMIT: "音频超过 200 MiB 上限，视频已保留。",
  STORAGE_FULL: "音频未保存：空间不足，请清理后重试。", DEPENDENCY_MISSING: "音频组件未安装完整，请检查安装说明。",
  TIMEOUT: "音频提取超时，可稍后重试。", INTERRUPTED: "音频提取已中断，可重试。",
  VERIFY_FAILED: "音频检查未通过，可重新提取。", AUDIO_FAILED: "音频提取失败，可稍后重试。",
  CLEANUP_FAILED: "音频临时文件未能清理，请检查保存位置。",
  RESULT_UNAVAILABLE: "原音频文件已移动或损坏，可重新提取。"
};
const cards = new Map();
const activeCards = new Map();
const existingCards = new Map();
let csrf = null, timer = null, searchTimer = null, requestVersion = 0;
let jobs = [], activeJobs = [], total = 0, preview = null, previewVersion = 0, remoteMode = false;
let existingJobId = null;
const batchSelection = { pending: new Set(), retry: new Set() };
const retryableStates = new Set(["failed", "cancelled", "expired"]);
const batchLabels = { queued: "已加入队列", saved: "已收藏", already_queued: "已在队列", invalid: "链接无效", queue_full: "队列已满", cancelled: "已取消", not_pending: "已开始或状态已变化，未取消", not_retryable: "当前状态无法重试", not_found: "记录已移除", failed: "未能处理" };
let batchBusy = false, submitting = false;
let unacceptedBatchLinks = [];

function notice(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

async function api(path, body) {
  const options = { cache: "no-store", mode: "same-origin", referrerPolicy: "same-origin", credentials: "same-origin", headers: {}, signal: AbortSignal.timeout(12000) };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrf || "";
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error("暂时无法连接光匣，请稍后重试。"); }
  const data = await response.json();
  if (response.status === 401 && data.error === "LOGIN_REQUIRED") window.location.replace("/login");
  if (!response.ok) throw Object.assign(new Error(data.message || "暂时无法完成请求，请稍后重试。"), { code: data.error, existing: data.existing });
  return data;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function inputLinks(text) {
  return (text.match(/https?:\/\/[^\s<>"'，。；！）】]+/gi) || []).map((url) => url.replace(/[.,;!)\]}]+$/, ""));
}

function updateInputCount() {
  const count = inputLinks($("work-input").value).length;
  $("input-count").textContent = `已识别 ${count} 条链接 · 一次最多 10 条${count > 10 ? "，请分批提交" : ""}`;
  $("input-count").classList.toggle("over-limit", count > 10);
  $("submit-button").textContent = count > 1 ? `加入队列（${count}）` : "开始下载";
}

function renderBatchResults(report, originals = [], unaccepted = null, inputPreserved = true) {
  // Queue operation reports must not replace the recovery copy of a submitted paste.
  if (unaccepted !== null) unacceptedBatchLinks = unaccepted;
  const list = $("batch-result-list");
  list.replaceChildren();
  for (const result of report.results) {
    const row = element("li", `batch-result ${result.status}`);
    row.append(element("span", "batch-result-status", batchLabels[result.status] || "未能处理"));
    row.append(element("span", "batch-result-source", result.url || originals[result.index - 1] || result.id || "链接无法识别"));
    if (result.existing) row.append(action(result.existing.hidden ? "恢复并查看" : "查看", "preview-button", () => showExisting(result.existing.id, result.existing.hidden)));
    list.append(row);
  }
  const summary = report.summary;
  const statuses = ["queued", "saved", "already_queued", "invalid", "queue_full", "cancelled", "not_pending", "not_retryable", "not_found", "failed"];
  $("batch-summary").textContent = `共 ${summary.total} 项 · ${statuses.filter((key) => summary[key]).map((key) => `${batchLabels[key]} ${summary[key]}`).join(" · ")} · 等待队列 ${report.pending_count} / ${report.pending_limit}`;
  $("batch-retained").textContent = unaccepted === null ? `之前提交的 ${unacceptedBatchLinks.length} 条未接收链接仍可追加回填，当前输入会保留。` : inputPreserved ? "未接收的链接已保留在输入框，可调整后再次提交。" : "你新编辑的文案已保留；未接收链接仍列在上方，可以追加回填到当前输入。";
  $("batch-retained").hidden = unacceptedBatchLinks.length === 0;
  $("refill-batch-links").hidden = unacceptedBatchLinks.length === 0;
  $("batch-results").hidden = false;
}

function selectionSize() { return batchSelection.pending.size + batchSelection.retry.size; }

function updateSelectionControls() {
  const pending = batchSelection.pending.size, retry = batchSelection.retry.size;
  $("cancel-selected").textContent = `取消选中待下载（${pending}）`;
  $("retry-selected").textContent = `重试选中未完成（${retry}）`;
  $("cancel-selected").disabled = batchBusy || pending === 0;
  $("retry-selected").disabled = batchBusy || retry === 0;
  for (const [scope, section] of [["pending", "active-selection"], ["retry", "history-selection"]]) {
    $(section).querySelector(".selection-count").textContent = `两个区域共选 ${selectionSize()} / 10 项`;
    const list = scope === "pending" ? $("active-list") : $("job-list");
    for (const label of list.querySelectorAll(".job-select")) {
      label.hidden = !$(section).open;
      const checkbox = label.querySelector("input");
      checkbox.checked = batchSelection[scope].has(checkbox.value);
      checkbox.disabled = batchBusy;
    }
  }
  $("select-pending").disabled = batchBusy || !activeJobs.some((job) => job.status === "pending");
  $("select-retryable").disabled = batchBusy || !jobs.some((job) => retryableStates.has(job.status));
  $("clear-pending-selection").disabled = batchBusy || pending === 0;
  $("clear-history-selection").disabled = batchBusy || retry === 0;
}

function clearHistorySelection() {
  batchSelection.retry.clear();
  updateSelectionControls();
}

function reconcileSelection() {
  let removed = 0;
  for (const [scope, list, eligible] of [["pending", activeJobs, (job) => job.status === "pending"], ["retry", jobs, (job) => retryableStates.has(job.status)]]) {
    const available = new Set(list.filter(eligible).map((job) => job.id));
    for (const id of batchSelection[scope]) if (!available.has(id)) { batchSelection[scope].delete(id); ++removed; }
  }
  if (removed) notice("batch-operation-notice", `${removed} 项已离开可选状态，本次选择已跳过。`);
}

function selectVisible(scope) {
  const eligible = scope === "pending" ? activeJobs.filter((job) => job.status === "pending") : jobs.filter((job) => retryableStates.has(job.status));
  for (const job of eligible) {
    if (selectionSize() >= 10) break;
    batchSelection[scope].add(job.id);
  }
  if (eligible.some((job) => !batchSelection[scope].has(job.id))) notice("batch-operation-notice", "最多同时选择 10 项，请分批处理。");
  updateSelectionControls();
}

async function runBatchAction(scope) {
  if (batchBusy || !batchSelection[scope].size) return;
  const ids = [...batchSelection[scope]];
  const originalJobs = new Map([...jobs, ...activeJobs].map((job) => [job.id, job]));
  batchBusy = true;
  updateSelectionControls();
  notice("action-error", "");
  try {
    const report = await api(`/api/jobs/batch/${scope === "pending" ? "cancel" : "retry"}`, { ids });
    // Keep rejected items selected when they remain eligible, so capacity failures are easy to retry.
    for (const result of report.results) if (["queued", "cancelled", "saved", "already_queued", "not_pending", "not_retryable", "not_found"].includes(result.status)) batchSelection[scope].delete(result.id);
    renderBatchResults(report, ids.map((id) => originalJobs.get(id)?.source_url || id));
    const counts = report.summary;
    const message = scope === "pending" ? `已取消 ${counts.cancelled || 0} 项` : `已加入队列 ${counts.queued || 0} 项`;
    const skipped = counts.total - (counts[scope === "pending" ? "cancelled" : "queued"] || 0);
    await refreshJobs();
    notice("batch-operation-notice", `${message}${skipped ? `，另有 ${skipped} 项请查看逐项结果。` : "。"}`);
  } catch (error) { notice("action-error", error.message); }
  finally { batchBusy = false; updateSelectionControls(); }
}

function size(bytes) {
  if (bytes >= 1024 ** 3) return `${Number((bytes / (1024 ** 3)).toFixed(1))} GiB`;
  if (bytes >= 1024 ** 2) return `${Number((bytes / (1024 ** 2)).toFixed(1))} MiB`;
  if (bytes >= 1024) return `${Number((bytes / 1024).toFixed(1))} KiB`;
  return `${Math.max(0, Math.round(bytes || 0))} 字节`;
}

function selectedLimit() {
  const value = Number($("video-limit").value);
  if (!Number.isInteger(value) || value < 1 || value > 8192) throw new Error("视频上限须为 1～8192 MiB 的整数。");
  return value;
}

function updateLimit() {
  try {
    const value = selectedLimit();
    $("limit-hint").textContent = `单视频上限 ${size(value * 1024 ** 2)}（可调）；单张图片 20 MiB、图集 200 MiB / 50 张。不限制视频时长。`;
    try { localStorage.setItem("lumabox.videoLimitMiB", String(value)); } catch {}
  } catch { $("limit-hint").textContent = "视频上限须为 1～8192 MiB 的整数；1024 MiB = 1 GiB。"; }
}

function failureText(job) {
  if (networkFailures[job.error_detail?.network_reason]) return networkFailures[job.error_detail.network_reason];
  if (job.error === "UNSUPPORTED_MEDIA" && mediaFailures[job.error_detail?.media_reason]) return mediaFailures[job.error_detail.media_reason];
  if (job.error !== "SIZE_LIMIT") return failures[job.error] || failures.DOWNLOAD_FAILED;
  const detail = job.error_detail || {};
  const limit = detail.limit_bytes || (job.video_limit_mib || 100) * 1024 ** 2;
  const amount = detail.actual_bytes || detail.at_least_bytes;
  const exact = amount && size(amount) === size(limit);
  const display = (bytes) => `${size(bytes)}${exact ? `（${bytes.toLocaleString("zh-CN")} 字节）` : ""}`;
  if (detail.actual_bytes) return `文件大小为 ${display(detail.actual_bytes)}，超过此任务的 ${display(limit)} 上限。`;
  if (detail.at_least_bytes) return `已读取文件超过 ${display(detail.at_least_bytes)}，此任务上限为 ${display(limit)}。`;
  if (job.kind === "gallery") return "图集超出限制：单张图片 20 MiB、整组 200 MiB，最多 50 张。";
  return `作品超出此任务的限制（视频上限 ${size(limit)}；图集 20 MiB / 张、200 MiB / 50 张）。可调整上限后重试。`;
}

function identity(job) {
  return job.work_id || (job.status === "running" ? "正在识别" : job.status === "pending" ? "等待识别" : "未识别作品");
}

function title(job) {
  return job.title || `${job.kind === "gallery" ? "图集" : job.kind === "video" ? "视频" : "作品"} · ${identity(job)}`;
}

function progressText(job) {
  const snapshot = job.progress || {};
  const parts = [stages[snapshot.stage] || "正在处理作品"];
  if (snapshot.stage === "media_transfer") {
    const received = snapshot.bytes_received;
    if (job.kind === "gallery") {
      if (Number.isFinite(snapshot.completed_count) && snapshot.expected_count > 0) parts.push(`已检查 ${snapshot.completed_count} / ${snapshot.expected_count} 张`);
      if (snapshot.resource_index > 0) parts.push(`正在下载第 ${snapshot.resource_index} 张`);
      if (Number.isFinite(received)) parts.push(`已接收 ${size(received)}`);
    } else if (Number.isFinite(received)) {
      if (snapshot.bytes_total > 0) parts.push(`${size(received)} / ${size(snapshot.bytes_total)} · ${Math.min(100, Math.floor(received / snapshot.bytes_total * 100))}%`);
      else parts.push(`已接收 ${size(received)}`);
    }
  }
  if (Number.isFinite(snapshot.elapsed_seconds)) parts.push(`已用时 ${Math.floor(snapshot.elapsed_seconds)} 秒`);
  return parts.join(" · ");
}

function action(text, className, perform, key) {
  const button = element("button", className, text);
  button.type = "button";
  button.dataset.focusKey = key || text;
  button.addEventListener("click", async () => {
    button.disabled = true;
    const errorTarget = button.closest("#existing-dialog") ? "existing-error" : "action-error";
    notice(errorTarget, "");
    try { await perform(); }
    catch (error) { notice(errorTarget, error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

async function addJob() {
  clearTimeout(searchTimer);
  clearHistorySelection();
  history.q = ""; history.status = "all"; history.page = 1;
  $("collection-search").value = "";
  $("collection-status").value = "all";
  await refreshJobs();
  notice("submit-success", remoteMode ? "已加入光匣，完成后可下载到本机。" : "已加入光匣，完成后自动保存在本机。");
}

async function showExisting(id, restore = false) {
  notice("existing-error", "");
  const job = restore ? await api(`/api/jobs/${encodeURIComponent(id)}/restore`, {}) : await api(`/api/jobs/${encodeURIComponent(id)}`);
  existingJobId = job.id;
  renderCardList($("existing-card"), [job], existingCards);
  if (!$("existing-dialog").open) $("existing-dialog").showModal();
  await refreshJobs();
  if (restore) window.dispatchEvent(new Event("lumabox:storage-changed"));
}

async function showDuplicate(job) {
  const current = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
  const target = current.duplicate_target;
  if (!target) throw new Error("已有收藏文件已不可用，可以重新下载。");
  if (target.hidden && !job.duplicate_target?.hidden && !window.confirm("这份收藏的记录已移除。恢复并查看吗？")) return;
  await showExisting(target.id, target.hidden);
}

function offerDuplicate(error, request) {
  if (!error.existing) return false;
  const existing = error.existing;
  $("duplicate-message").textContent = error.message;
  const controls = $("duplicate-actions");
  controls.replaceChildren(action(existing.hidden ? "恢复并查看" : "查看已有任务", "preview-button", () => showExisting(existing.id, existing.hidden)));
  if (request.include_audio && ["video", "gallery"].includes(existing.kind) && existing.match_type === "saved") controls.append(action(existing.kind === "gallery" ? "补保存配乐" : "补充提取音频", "preview-button", async () => {
    if (existing.hidden) await api(`/api/jobs/${encodeURIComponent(existing.id)}/restore`, {});
    await api(`/api/jobs/${encodeURIComponent(existing.id)}/audio`, {});
    $("duplicate-notice").hidden = true;
    await addJob();
    notice("submit-success", "已为已有作品安排音频保存。");
  }));
  if (existing.match_type === "saved") controls.append(action("重新下载", "retry-button", async () => {
    if (!window.confirm("重新下载会保留已有文件，并额外占用空间。继续吗？")) return;
    await api("/api/jobs", { ...request, force_download: true });
    $("duplicate-notice").hidden = true;
    if ($("work-input").value.trim() === request.input) $("work-input").value = "";
    updateInputCount();
    await addJob();
  }));
  $("duplicate-notice").hidden = false;
  return true;
}

function buildCard(job, selectionScope = null) {
  const gallery = job.kind === "gallery";
  const card = element("article", "job-card");
  card.dataset.jobId = job.id;
  card.append(element("div", `job-icon ${gallery ? "gallery" : ""}`, gallery ? "▧" : "▶"));
  const body = element("div", "job-body"), top = element("div", "job-top");
  if ((selectionScope === "pending" && job.status === "pending") || (selectionScope === "retry" && retryableStates.has(job.status))) {
    const label = element("label", "job-select");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.value = job.id;
    checkbox.dataset.focusKey = "batch-select";
    checkbox.setAttribute("aria-label", `选择${selectionScope === "pending" ? "待下载" : "可重试"}任务：${title(job)}`);
    checkbox.checked = batchSelection[selectionScope].has(job.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && selectionSize() >= 10) {
        checkbox.checked = false;
        notice("batch-operation-notice", "最多同时选择 10 项，请分批处理。");
      } else if (checkbox.checked) batchSelection[selectionScope].add(job.id);
      else batchSelection[selectionScope].delete(job.id);
      updateSelectionControls();
    });
    label.append(checkbox, document.createTextNode(selectionScope === "pending" ? "选择待下载" : "选择重试"));
    body.append(label);
  }
  top.append(element("h3", "job-title", title(job)));
  top.append(element("span", `job-status ${job.status}`, labels[job.status] || "等待更新"));
  body.append(top);
  const date = new Date(job.created_at * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const savedBytes = job.files.filter((file) => file.mime !== "application/zip").reduce((sum, file) => sum + file.bytes, 0);
  body.append(element("p", "job-meta", `${date}${savedBytes ? ` · ${size(savedBytes)}` : " 添加"}`));
  const more = element("details", "job-more");
  const moreSummary = element("summary", "", "更多");
  moreSummary.dataset.focusKey = "more";
  more.append(moreSummary);
  const moreControls = element("div", "job-controls more-controls");
  const technical = element("details", "job-details");
  const technicalSummary = element("summary", "", "任务详情");
  technicalSummary.dataset.focusKey = "details";
  technical.append(technicalSummary);
  if (job.work_id) technical.append(element("p", "job-meta", `作品 ID：${job.work_id}`));
  if (job.source_url) technical.append(element("p", "job-source", job.source_url));
  if (job.attempt_count) technical.append(element("p", "job-meta", `第 ${job.attempt_count} / ${job.max_attempts || 2} 次尝试`));
  let detail = "";
  if (job.status === "running") detail = progressText(job);
  if (job.status === "pending") detail = "已加入队列，稍后开始。";
  if (job.status === "retry_wait") detail = `${failureText(job)} ${Math.max(0, Math.ceil(job.retry_at - Date.now() / 1000))} 秒后自动重试，可随时取消。`;
  if (job.status === "duplicate") detail = job.duplicate_target ? "此作品已有收藏，本次未保留重复文件。" : "已有收藏文件已不可用，可以重新下载。";
  if (job.status === "cancelling") detail = "正在停止下载并清理未完成文件，请稍候。";
  if (job.status === "cancelled") detail = "下载已取消。需要时可以重新下载。";
  if (job.status === "expired") detail = "旧版临时文件已过期，可以重新保存此作品。";
  if (job.status === "failed") detail = failureText(job);
  if (job.status === "success") {
    const media = job.files.filter((file) => file.mime !== "application/zip" && !isAudioFile(file));
    const motions = gallery ? media.filter((file) => file.mime === "video/mp4").length : 0;
    const description = gallery ? `${job.actual_count} 张图片${motions ? ` · ${motions} 段动态片段` : ""}` : "完整视频";
    detail = `${description} · ${remoteMode ? "已保存到服务器" : "已保存到本机"}`;
    if (job.storage_state !== "present") detail = "文件删除尚未完成，请到文件管理中处理。";
  }
  if (detail) body.append(element("p", `job-detail ${job.status}`, detail));
  if (job.attempts?.length) {
    const log = element("details", "attempt-log");
    log.append(element("summary", "", "尝试记录"));
    for (const attempt of job.attempts) log.append(element("p", "job-meta", `第 ${attempt.number} 次：${attempt.error ? failureText({error: attempt.error, error_detail: attempt.detail}) : "文件检查通过"}`));
    technical.append(log);
  }
  if (job.status === "failed" && networkFailures[job.error_detail?.network_reason]) {
    const locations = { dns_resolution: "域名解析", page_navigation: "读取作品页面", media_transfer: "下载媒体" };
    const location = [locations[job.error_detail.stage], job.error_detail.host].filter(Boolean).join(" · ");
    if (location) technical.append(element("p", "job-meta", `失败位置：${location}`));
  }
  const snapshot = job.progress || {};
  if (job.status === "running" && job.kind !== "gallery" && snapshot.stage === "media_transfer" && snapshot.bytes_total > 0 && Number.isFinite(snapshot.bytes_received)) {
    const meter = element("progress", "job-progress-meter");
    meter.max = snapshot.bytes_total;
    meter.value = snapshot.bytes_received;
    meter.setAttribute("aria-label", "下载进度");
    body.append(meter);
  }
  const controls = element("div", "job-controls");
  const audio = job.audio || { status: "none" };
  const audioActive = activeAudio.has(audio.status);
  const galleryAudioMessages = {
    waiting: "图片完成后将自动保存背景音乐。", pending: "背景音乐已排队，稍后开始。",
    running: "正在保存背景音乐…", cancelling: "正在取消背景音乐…",
    unsupported: "图片已保存，可单独保存配乐。",
    cancelled: "背景音乐已取消，图片已保留。", success: "配乐已保存 · MP3"
  };
  const galleryAudioFailures = {
    NO_AUDIO: "此图集没有可用的背景音乐。", SIZE_LIMIT: "背景音乐超过 200 MiB 上限。",
    AUTH_REQUIRED: "背景音乐需要登录后重试。", CHALLENGE_REQUIRED: "背景音乐需要完成验证后重试。",
    BLOCKED: "背景音乐暂时无法访问。", RATE_LIMITED: "背景音乐请求过于频繁，请稍后重试。",
    NOT_FOUND: "背景音乐已不可用。", UNSUPPORTED_MEDIA: "背景音乐格式暂不支持。",
    DOWNLOAD_FAILED: "背景音乐下载失败，请稍后重试。",
    UNSAFE_TARGET: "配乐地址校验未通过，请检查网络后重试。"
  };
  const diagnosticText = audio.status === "failed" ? audioDiagnosticText(audio.detail) : null;
  let audioText = audio.status === "failed" ? diagnosticText || audioFailures[audio.error] || audioFailures.AUDIO_FAILED : audioMessages[audio.status];
  if (!job.kind && audio.status === "waiting") audioText = "作品完成后将自动保存音频。";
  if (gallery) {
    audioText = audio.status === "failed"
      ? `${diagnosticText || galleryAudioFailures[audio.error] || (audioFailures[audio.error] || audioFailures.AUDIO_FAILED).replaceAll("视频已保留。", "")} 图片已保留。`
      : galleryAudioMessages[audio.status] || audioText;
  }
  if (audioText) body.append(element("p", `audio-status ${audio.status}`, audioText));
  if (["pending", "running"].includes(audio.status)) controls.append(action("取消音频", "cancel-button", async () => {
    await api(`/api/jobs/${encodeURIComponent(job.id)}/audio/cancel`, {});
    await refreshJobs();
  }, "cancel-audio"));
  if (["pending", "running", "retry_wait"].includes(job.status)) controls.append(action("取消下载", "cancel-button", async () => {
    await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {});
    await refreshJobs();
  }, "cancel"));
  if (job.status === "success" && job.storage_state === "present") {
    if (job.output_directory) technical.append(element("p", "job-path", `${remoteMode ? "服务器保存位置" : "保存位置"}：${job.output_directory}`));
    const media = job.files.filter((file) => ["video/mp4", "image/jpeg", "image/webp"].includes(file.mime));
    if (media.length) controls.append(action(gallery ? "预览图集" : "预览视频", "preview-button", () => openPreview(job), "preview"));
    {
      const primary = job.files.find((file) => file.mime === (gallery ? "application/zip" : "video/mp4"));
      if (primary) {
        const link = element("a", "download-link primary", gallery ? "下载整组 ZIP" : "下载视频");
        link.href = `/api/files/${encodeURIComponent(primary.id)}`;
        link.download = primary.name;
        link.dataset.focusKey = "download-primary";
        controls.append(link);
      }
    }
    if (!remoteMode && job.output_directory) moreControls.append(action("打开文件夹", "folder-button", async () => {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/open-folder`, {});
      notice("submit-success", "已打开保存文件夹。");
    }, "folder"));
    const audioFile = job.files.find((file) => isAudioFile(file));
    if (audioFile) {
      moreControls.append(action("试听音频", "preview-button", () => openPreview(job, "audio"), "audio-preview"));
      const link = element("a", "download-link", "下载音频");
      link.href = `/api/files/${encodeURIComponent(audioFile.id)}`;
      link.download = audioFile.name;
      link.dataset.focusKey = "download-audio";
      moreControls.append(link);
    } else if (!audioActive) moreControls.append(action(gallery ? (audio.status === "failed" ? "重试配乐" : "保存配乐") : (audio.status === "failed" ? "重新提取音频" : "提取音频"), "preview-button", async () => {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/audio`, {});
      await refreshJobs();
    }, "extract-audio"));
    const extra = element("details", "save-extra");
    const summary = element("summary", "", "另存文件");
    summary.dataset.focusKey = "save";
    extra.append(summary);
    const links = element("div", "file-links");
    const ordered = [...job.files].sort((a, b) => (b.mime === "application/zip") - (a.mime === "application/zip"));
    for (const file of ordered.filter((file) => !isAudioFile(file))) {
      const name = file.mime === "application/zip" ? "另存整组 ZIP" : gallery ? (file.mime === "video/mp4" ? `另存动态片段 ${file.name.split("-")[0]}` : `另存图片 ${file.name.split(".")[0]}`) : "另存视频";
      const link = element("a", "download-link", name);
      link.href = `/api/files/${encodeURIComponent(file.id)}`;
      link.download = file.name;
      link.dataset.focusKey = `file-${file.id}`;
      links.append(link);
    }
    extra.append(links);
    if (!remoteMode || gallery) moreControls.append(extra);
  }
  if (job.status === "duplicate" && job.duplicate_target) controls.append(action(job.duplicate_target.hidden ? "恢复并查看" : "查看已有收藏", "preview-button", () => showDuplicate(job)));
  if (["failed", "expired", "cancelled", "duplicate"].includes(job.status)) controls.append(action(job.status === "cancelled" ? "重新下载" : "一键重试", "retry-button", async () => {
    const request = { input: job.source_url, video_limit_mib: selectedLimit() };
    try { await api(`/api/jobs/${encodeURIComponent(job.id)}/retry`, { video_limit_mib: request.video_limit_mib }); await addJob(); }
    catch (error) { if (!offerDuplicate(error, request)) throw error; }
  }, "retry"));
  if (["success", "failed", "expired", "cancelled", "duplicate"].includes(job.status) && !audioActive) moreControls.append(action("移除记录", "remove-button", async () => {
    if (!window.confirm(`移除这条收藏记录？已保存的文件会保留在${remoteMode ? "服务器" : "电脑"}上。`)) return;
    await api(`/api/jobs/${encodeURIComponent(job.id)}/remove`, {});
    await refreshJobs();
    window.dispatchEvent(new Event("lumabox:storage-changed"));
  }, "remove"));
  if (controls.childElementCount) body.append(controls);
  if (moreControls.childElementCount) more.append(moreControls);
  if (technical.childElementCount > 1) more.append(technical);
  if (more.childElementCount > 1) body.append(more);
  card.append(body);
  return card;
}

function renderJobs() {
  reconcileSelection();
  const focused = document.activeElement;
  const focusJob = focused?.closest("#workspace-view [data-job-id]")?.dataset.jobId;
  const focusKey = focused?.dataset.focusKey;
  for (const [items, destination, source] of [[jobs, cards, activeCards], [activeJobs, activeCards, cards]]) {
    for (const job of items) if (source.has(job.id)) {
      destination.set(job.id, source.get(job.id));
      source.delete(job.id);
    }
  }
  $("task-count").textContent = `${total} 份收藏`;
  $("active-tasks").hidden = activeJobs.length === 0;
  $("active-count").textContent = `${activeJobs.length} 个任务`;
  $("empty-state").hidden = jobs.length !== 0;
  $("empty-state").querySelector("h3").textContent = history.q || history.status !== "all" ? "没有找到这份收藏" : "光匣里还很安静";
  $("empty-state").querySelector("p").textContent = history.q || history.status !== "all" ? "试试其他关键词，或切换任务状态。" : activeJobs.length ? "完成的收藏会显示在这里。" : "添加第一条链接，把喜欢的瞬间留下来。";
  const pageCount = Math.max(1, Math.ceil(total / history.page_size));
  $("page-info").textContent = `第 ${history.page} / ${pageCount} 页`;
  $("previous-page").disabled = history.page <= 1;
  $("next-page").disabled = history.page >= pageCount;
  renderCardList($("job-list"), jobs, cards, "retry");
  renderCardList($("active-list"), activeJobs, activeCards, "pending");
  updateSelectionControls();
  if (focusJob && focusKey) {
    const entry = cards.get(focusJob) || activeCards.get(focusJob);
    [...(entry?.node.querySelectorAll("[data-focus-key]") || [])].find((item) => item.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
  }
}

function renderCardList(list, items, entries, selectionScope = null) {
  const visible = new Set(items.map((job) => job.id));
  for (const [id, entry] of entries) {
    if (!visible.has(id)) { entry.node.remove(); entries.delete(id); }
  }
  items.forEach((job, index) => {
    const signature = `${selectionScope}:` + JSON.stringify(job);
    let entry = entries.get(job.id);
    if (!entry || entry.signature !== signature) {
      const node = buildCard(job, selectionScope);
      const expanded = [...(entry?.node.querySelectorAll("details") || [])].filter((item) => item.open).map((item) => item.className);
      const focusKey = entry?.node.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
      for (const className of expanded) {
        const detail = [...node.querySelectorAll("details")].find((item) => item.className === className);
        if (detail) detail.open = true;
      }
      if (entry) entry.node.replaceWith(node);
      entry = { node, signature };
      entries.set(job.id, entry);
      if (focusKey) [...node.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
    }
    if (list.children[index] !== entry.node) list.insertBefore(entry.node, list.children[index] || null);
  });
}

async function refreshJobs() {
  if (!csrf) return;
  const version = ++requestVersion;
  const query = new URLSearchParams(history);
  try {
    const existingId = existingJobId;
    const [result, active, existing] = await Promise.all([
      api(`/api/jobs?${query}`), api("/api/jobs/active"),
      existingId && $("existing-dialog").open ? api(`/api/jobs/${encodeURIComponent(existingId)}`).catch(() => null) : null
    ]);
    if (version !== requestVersion) return;
    const previousStates = new Map([...jobs, ...activeJobs].map((job) => [job.id, `${job.status}/${job.audio?.status}`]));
    jobs = result.jobs;
    activeJobs = active.jobs;
    total = result.total;
    history.page = result.page;
    if (!jobs.length && history.page > 1) {
      history.page = Math.max(1, Math.ceil(total / history.page_size));
      return refreshJobs();
    }
    renderJobs();
    if (existing && existingJobId === existingId && $("existing-dialog").open) renderCardList($("existing-card"), [existing], existingCards);
    if ([...jobs, ...activeJobs].some((job) => ["success", "failed", "cancelled", "duplicate"].includes(job.status) && previousStates.get(job.id) !== `${job.status}/${job.audio?.status}`)) window.dispatchEvent(new Event("lumabox:storage-changed"));
    notice("connection-error", "");
  } catch {
    if (version === requestVersion) notice("connection-error", "连接暂时中断，正在尝试重新连接…");
  }
}

async function poll() {
  await refreshJobs();
  timer = setTimeout(poll, 2000);
}

function releasePreview() {
  ++previewVersion;
  preview = null;
  clearPreviewMedia();
}

function clearPreviewMedia() {
  for (const id of ["preview-video", "preview-audio"]) {
    $(id).pause();
    $(id).removeAttribute("src");
    $(id).load();
  }
  $("preview-image").removeAttribute("src");
}

async function showPreviewFile() {
  if (!preview) return;
  const version = ++previewVersion, file = preview.files[preview.index];
  const path = `/api/files/${encodeURIComponent(file.id)}/preview`;
  const gallery = preview.kind === "gallery";
  const audio = isAudioFile(file);
  const image = file.mime.startsWith("image/");
  clearPreviewMedia();
  $("preview-video").hidden = true;
  $("preview-audio").hidden = true;
  $("preview-image").hidden = true;
  $("gallery-navigation").hidden = !gallery;
  $("previous-image").disabled = preview.index === 0;
  $("next-image").disabled = preview.index === preview.files.length - 1;
  $("image-counter").textContent = `${preview.index + 1} / ${preview.files.length}`;
  notice("preview-error", "");
  $("preview-loading").hidden = false;
  try {
    const response = await fetch(path, { method: "HEAD", cache: "no-store", mode: "same-origin", credentials: "same-origin", signal: AbortSignal.timeout(12000) });
    if (version !== previewVersion) return;
    if (!response.ok) throw new Error("文件可能已移动或删除，请检查保存位置后重新下载。");
    const media = image ? $("preview-image") : audio ? $("preview-audio") : $("preview-video");
    if (image) media.alt = `图片 ${preview.index + 1} / ${preview.files.length} · ${file.name}`;
    media.src = path;
    media.hidden = false;
  } catch (error) {
    if (version === previewVersion) notice("preview-error", error.message || "暂时无法读取文件，请稍后重试。");
  } finally {
    if (version === previewVersion) $("preview-loading").hidden = true;
  }
}

async function openPreview(job, mode = "media") {
  releasePreview();
  preview = { kind: mode === "audio" ? "audio" : job.kind, index: 0, files: job.files.filter((file) => mode === "audio" ? isAudioFile(file) : ["video/mp4", "image/jpeg", "image/webp"].includes(file.mime)) };
  if (!preview.files.length) return;
  $("preview-title").textContent = title(job);
  $("preview-identity").textContent = job.work_id ? `作品 ID ${job.work_id}` : "";
  $("preview-dialog").showModal();
  await showPreviewFile();
}

function moveImage(offset) {
  if (!preview || preview.kind !== "gallery") return;
  const next = preview.index + offset;
  if (next < 0 || next >= preview.files.length) return;
  preview.index = next;
  showPreviewFile();
}

function changePage(offset) {
  const lastPage = Math.max(1, Math.ceil(total / history.page_size));
  const nextPage = Math.max(1, Math.min(lastPage, history.page + offset));
  if (nextPage === history.page) return;
  clearHistorySelection();
  history.page = nextPage;
  $("previous-page").disabled = true;
  $("next-page").disabled = true;
  refreshJobs();
}

$("close-preview").addEventListener("click", () => $("preview-dialog").close());
$("close-existing").addEventListener("click", () => $("existing-dialog").close());
$("existing-dialog").addEventListener("close", () => { existingJobId = null; existingCards.clear(); $("existing-card").replaceChildren(); });
$("preview-dialog").addEventListener("close", releasePreview);
$("preview-dialog").addEventListener("keydown", (event) => {
  if (preview?.kind === "gallery" && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    moveImage(event.key === "ArrowLeft" ? -1 : 1);
  }
});
$("previous-image").addEventListener("click", () => moveImage(-1));
$("next-image").addEventListener("click", () => moveImage(1));
for (const id of ["preview-video", "preview-audio", "preview-image"]) $(id).addEventListener("error", () => {
  if (preview) notice("preview-error", remoteMode ? "无法预览此文件，请尝试下载文件。" : "无法预览此文件，请尝试打开保存文件夹或另存文件。");
});

$("collection-search").addEventListener("input", () => {
  clearHistorySelection();
  history.q = $("collection-search").value.trim(); history.page = 1;
  ++requestVersion;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refreshJobs, 250);
});
$("collection-status").addEventListener("change", () => {
  clearHistorySelection();
  history.status = $("collection-status").value; history.page = 1;
  refreshJobs();
});
$("previous-page").addEventListener("click", () => changePage(-1));
$("next-page").addEventListener("click", () => changePage(1));
$("clear-failed").addEventListener("click", async () => {
  $("clear-failed").disabled = true;
  notice("action-error", "");
  try {
    const result = await api("/api/jobs/clear-failed", {});
    window.dispatchEvent(new Event("lumabox:storage-changed"));
    await refreshJobs();
    notice("submit-success", `已清理 ${result.removed} 条失败记录，已保存的文件保留在${remoteMode ? "服务器" : "电脑"}上。`);
  } catch (error) { notice("action-error", error.message); }
  finally { $("clear-failed").disabled = false; }
});
$("video-limit").addEventListener("input", updateLimit);
$("work-input").addEventListener("input", updateInputCount);
$("refill-batch-links").addEventListener("click", () => {
  const current = $("work-input").value.trim();
  const present = new Set(inputLinks(current));
  const additions = unacceptedBatchLinks.filter((url) => !present.has(url));
  const text = [current, ...additions].filter(Boolean).join("\n");
  if (text.length > 4096) {
    notice("submit-error", "追加后超过 4096 字，请先处理当前输入；未接收链接仍可通过回填按钮取回。");
    return;
  }
  $("work-input").value = text;
  updateInputCount();
  $("work-input").focus();
});
$("active-selection").addEventListener("toggle", updateSelectionControls);
$("history-selection").addEventListener("toggle", updateSelectionControls);
$("select-pending").addEventListener("click", () => selectVisible("pending"));
$("select-retryable").addEventListener("click", () => selectVisible("retry"));
$("clear-pending-selection").addEventListener("click", () => { batchSelection.pending.clear(); updateSelectionControls(); });
$("clear-history-selection").addEventListener("click", clearHistorySelection);
$("cancel-selected").addEventListener("click", () => runBatchAction("pending"));
$("retry-selected").addEventListener("click", () => runBatchAction("retry"));
$("video-limit").addEventListener("invalid", (event) => {
  event.preventDefault();
  $("download-options").open = true;
  notice("submit-error", "视频上限须为 1～8192 MiB 的整数。");
  $("video-limit").focus();
});
$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting) return;
  submitting = true;
  $("submit-button").disabled = true;
  notice("submit-error", ""); notice("submit-success", "");
  $("duplicate-notice").hidden = true;
  let request;
  try {
    request = { input: $("work-input").value.trim(), video_limit_mib: selectedLimit(), include_audio: $("include-audio").checked };
    const links = inputLinks(request.input);
    if (links.length > 10) throw new Error("一次最多提交 10 条链接，请分批添加；重复链接也计入数量。");
    if (links.length > 1) {
      const report = await api("/api/jobs/batch", request);
      const unaccepted = report.results.filter((result) => !["queued", "saved", "already_queued"].includes(result.status)).map((result) => links[result.index - 1]);
      // Never overwrite edits made while the request was in flight.
      const inputPreserved = $("work-input").value.trim() === request.input;
      if (inputPreserved) $("work-input").value = unaccepted.join("\n");
      renderBatchResults(report, links, unaccepted, inputPreserved);
      if (report.summary.queued) await addJob();
      else await refreshJobs();
      notice("submit-success", `已加入队列 ${report.summary.queued || 0} 项${unaccepted.length ? `，${unaccepted.length} 条未接收链接${inputPreserved ? "保留在输入框" : "可从结果中回填"}。` : "。"}`);
    } else {
      await api("/api/jobs", request);
      if ($("work-input").value.trim() === request.input) $("work-input").value = "";
      $("batch-results").hidden = true;
      await addJob();
    }
    updateInputCount();
  } catch (error) { if (!request || !offerDuplicate(error, request)) notice("submit-error", error.message); }
  finally { submitting = false; $("submit-button").disabled = false; }
});

async function start() {
  try {
    const settings = await api("/api/session");
    csrf = settings.csrf;
    $("logout-button").hidden = settings.access_protected !== true;
    remoteMode = settings.remote_mode === true;
    if (remoteMode) {
      $("workspace-label").textContent = "云端工作台";
      $("workspace-description").textContent = "粘贴链接，云端保存，随时下载到你的电脑。";
      $("storage-heading").textContent = "收藏，保存在云端。";
      $("storage-description").textContent = "视频保存为 MP4，图集按作品顺序打包。文件检查通过后，即可预览或下载到本机。";
      $("output-label").textContent = "服务器保存位置";
    }
    let remembered;
    try { remembered = Number(localStorage.getItem("lumabox.videoLimitMiB")); } catch {}
    $("video-limit").value = Number.isInteger(remembered) && remembered >= 1 && remembered <= 8192 ? remembered : settings.video_limit_mib;
    $("output-root").textContent = settings.output_directory;
    updateLimit();
    $("submit-button").disabled = false;
    $("clear-failed").disabled = false;
    window.LumaBoxStorage.init({ request: api, formatSize: size, onChanged: refreshJobs });
    notice("connection-error", "");
    poll();
  } catch {
    notice("connection-error", "暂时无法连接光匣，正在尝试重新连接…");
    timer = setTimeout(start, 3000);
  }
}
$("logout-button").addEventListener("click", async () => {
  $("logout-button").disabled = true;
  try {
    await api("/auth/logout", {});
    clearTimeout(timer);
    window.location.replace("/login");
  } catch (error) {
    notice("action-error", error.message);
    $("logout-button").disabled = false;
  }
});
start();
