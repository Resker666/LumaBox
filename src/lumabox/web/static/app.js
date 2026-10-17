"use strict";
const $ = (id) => document.getElementById(id);
const labels = { pending: "待下载", running: "下载中", cancelling: "正在取消", cancelled: "已取消", success: "已完成", failed: "未完成", expired: "已过期" };
const stages = { browser_start: "启动浏览器", browser_load: "读取作品", media_transfer: "下载媒体", media_verification: "检查文件", publication: "整理收藏" };
const failures = {
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
const mediaFailures = {
  TARGET_UNCONFIRMED: "页面已打开，但尚未确认目标作品区域，暂未保存文件。",
  WORK_CHANGED: "页面切换到了其他作品，下载已停止，请重新提交原链接。",
  MEDIA_COUNT_UNCONFIRMED: "已识别作品页面，但无法确认完整页数，暂未保存文件。",
  MEDIA_ORDER_UNCONFIRMED: "作品图片尚未全部加载，或无法确认完整顺序，暂未保存文件。",
  MEDIA_URL_UNAVAILABLE: "播放器尚未提供可下载的完整媒体地址，请稍后重试。",
  MOTION_URL_UNAVAILABLE: "已识别动态照片，但动态片段尚未加载完整，请稍后重试。",
  IMAGES_LOADING: "作品仍在显示默认占位图片，请稍后重试。"
};
const history = { q: "", status: "all", page: 1, page_size: 20 };
const cards = new Map();
let csrf = null, timer = null, searchTimer = null, requestVersion = 0;
let jobs = [], total = 0, preview = null, previewVersion = 0, remoteMode = false;

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
  if (!response.ok) throw new Error(data.message || "暂时无法完成请求，请稍后重试。");
  return data;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
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
    notice("action-error", "");
    try { await perform(); }
    catch (error) { notice("action-error", error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

async function addJob() {
  clearTimeout(searchTimer);
  history.q = ""; history.status = "all"; history.page = 1;
  $("collection-search").value = "";
  $("collection-status").value = "all";
  await refreshJobs();
  notice("submit-success", remoteMode ? "已加入光匣，完成后可下载到本机。" : "已加入光匣，完成后自动保存在本机。");
}

function buildCard(job) {
  const gallery = job.kind === "gallery";
  const card = element("article", "job-card");
  card.dataset.jobId = job.id;
  card.append(element("div", `job-icon ${gallery ? "gallery" : ""}`, gallery ? "▧" : "▶"));
  const body = element("div", "job-body"), top = element("div", "job-top");
  top.append(element("h3", "job-title", title(job)));
  top.append(element("span", `job-status ${job.status}`, labels[job.status] || "等待更新"));
  body.append(top);
  const date = new Date(job.created_at * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  body.append(element("p", "job-meta", `${date} 添加${job.title && job.work_id ? ` · 作品 ID ${job.work_id}` : ""}`));
  if (job.source_url) body.append(element("p", "job-source", job.source_url));
  let detail = "";
  if (job.status === "running") detail = progressText(job);
  if (job.status === "pending") detail = "已加入队列，稍后开始。";
  if (job.status === "cancelling") detail = "正在停止下载并清理未完成文件，请稍候。";
  if (job.status === "cancelled") detail = "下载已取消。需要时可以重新下载。";
  if (job.status === "expired") detail = "旧版临时文件已过期，可以重新保存此作品。";
  if (job.status === "failed") detail = failureText(job);
  if (job.status === "success") {
    const media = job.files.filter((file) => file.mime !== "application/zip");
    const motions = gallery ? media.filter((file) => file.mime === "video/mp4").length : 0;
    const description = gallery ? `${job.actual_count} 张图片${motions ? ` · ${motions} 段动态片段` : ""}` : "完整视频";
    detail = `${description} · ${size(media.reduce((sum, file) => sum + file.bytes, 0))} · ${remoteMode ? "已保存到服务器" : "已保存到本机"}`;
  }
  if (detail) body.append(element("p", `job-detail ${job.status}`, detail));
  if (job.status === "failed" && networkFailures[job.error_detail?.network_reason]) {
    const locations = { dns_resolution: "域名解析", page_navigation: "读取作品页面", media_transfer: "下载媒体" };
    const location = [locations[job.error_detail.stage], job.error_detail.host].filter(Boolean).join(" · ");
    if (location) body.append(element("p", "job-meta", `失败位置：${location}`));
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
  if (["pending", "running"].includes(job.status)) controls.append(action("取消下载", "cancel-button", async () => {
    await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, {});
    await refreshJobs();
  }, "cancel"));
  if (job.status === "success") {
    if (job.output_directory) body.append(element("p", "job-path", `${remoteMode ? "服务器保存位置" : "保存位置"}：${job.output_directory}`));
    const media = job.files.filter((file) => ["video/mp4", "image/jpeg", "image/webp"].includes(file.mime));
    if (media.length) controls.append(action(gallery ? "预览图集" : "预览视频", "preview-button", () => openPreview(job), "preview"));
    if (remoteMode) {
      const primary = job.files.find((file) => file.mime === (gallery ? "application/zip" : "video/mp4"));
      if (primary) {
        const link = element("a", "download-link primary", gallery ? "下载整组 ZIP" : "下载视频");
        link.href = `/api/files/${encodeURIComponent(primary.id)}`;
        link.download = primary.name;
        controls.append(link);
      }
    }
    if (!remoteMode && job.output_directory) controls.append(action("打开文件夹", "folder-button", async () => {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/open-folder`, {});
      notice("submit-success", "已打开保存文件夹。");
    }, "folder"));
    const extra = element("details", "save-extra");
    const summary = element("summary", "", "另存文件");
    summary.dataset.focusKey = "save";
    extra.append(summary);
    const links = element("div", "file-links");
    const ordered = [...job.files].sort((a, b) => (b.mime === "application/zip") - (a.mime === "application/zip"));
    for (const file of ordered) {
      const name = file.mime === "application/zip" ? "另存整组 ZIP" : gallery ? (file.mime === "video/mp4" ? `另存动态片段 ${file.name.split("-")[0]}` : `另存图片 ${file.name.split(".")[0]}`) : "另存视频";
      const link = element("a", "download-link", name);
      link.href = `/api/files/${encodeURIComponent(file.id)}`;
      link.download = file.name;
      link.dataset.focusKey = `file-${file.id}`;
      links.append(link);
    }
    extra.append(links);
    if (!remoteMode || gallery) controls.append(extra);
  }
  if (["failed", "expired", "cancelled"].includes(job.status)) controls.append(action(job.status === "cancelled" ? "重新下载" : "一键重试", "retry-button", async () => {
    await api(`/api/jobs/${encodeURIComponent(job.id)}/retry`, { video_limit_mib: selectedLimit() });
    await addJob();
  }, "retry"));
  if (["success", "failed", "expired", "cancelled"].includes(job.status)) controls.append(action("移除记录", "remove-button", async () => {
    if (!window.confirm(`移除这条收藏记录？已保存的文件会保留在${remoteMode ? "服务器" : "电脑"}上。`)) return;
    await api(`/api/jobs/${encodeURIComponent(job.id)}/remove`, {});
    await refreshJobs();
  }, "remove"));
  if (controls.childElementCount) body.append(controls);
  card.append(body);
  return card;
}

function renderJobs() {
  $("task-count").textContent = `${total} 份收藏`;
  $("empty-state").hidden = jobs.length !== 0;
  $("empty-state").querySelector("h3").textContent = history.q || history.status !== "all" ? "没有找到这份收藏" : "光匣里还很安静";
  $("empty-state").querySelector("p").textContent = history.q || history.status !== "all" ? "试试其他关键词，或切换任务状态。" : "添加第一条链接，把喜欢的瞬间留下来。";
  const pageCount = Math.max(1, Math.ceil(total / history.page_size));
  $("page-info").textContent = `第 ${history.page} / ${pageCount} 页`;
  $("previous-page").disabled = history.page <= 1;
  $("next-page").disabled = history.page >= pageCount;
  const list = $("job-list"), visible = new Set(jobs.map((job) => job.id));
  for (const [id, entry] of cards) {
    if (!visible.has(id)) { entry.node.remove(); cards.delete(id); }
  }
  jobs.forEach((job, index) => {
    const signature = JSON.stringify(job);
    let entry = cards.get(job.id);
    if (!entry || entry.signature !== signature) {
      const node = buildCard(job);
      const expanded = entry?.node.querySelector("details")?.open;
      const focusKey = entry?.node.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
      if (expanded && node.querySelector("details")) node.querySelector("details").open = true;
      if (entry) entry.node.replaceWith(node);
      entry = { node, signature };
      cards.set(job.id, entry);
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
    const result = await api(`/api/jobs?${query}`);
    if (version !== requestVersion) return;
    jobs = result.jobs;
    total = result.total;
    history.page = result.page;
    if (!jobs.length && history.page > 1) {
      history.page = Math.max(1, Math.ceil(total / history.page_size));
      return refreshJobs();
    }
    renderJobs();
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
  $("preview-video").pause();
  $("preview-video").removeAttribute("src");
  $("preview-video").load();
  $("preview-image").removeAttribute("src");
}

async function showPreviewFile() {
  if (!preview) return;
  const version = ++previewVersion, file = preview.files[preview.index];
  const path = `/api/files/${encodeURIComponent(file.id)}/preview`;
  const gallery = preview.kind === "gallery";
  const image = file.mime !== "video/mp4";
  clearPreviewMedia();
  $("preview-video").hidden = true;
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
    const media = image ? $("preview-image") : $("preview-video");
    if (image) media.alt = `图片 ${preview.index + 1} / ${preview.files.length} · ${file.name}`;
    media.src = path;
    media.hidden = false;
  } catch (error) {
    if (version === previewVersion) notice("preview-error", error.message || "暂时无法读取文件，请稍后重试。");
  } finally {
    if (version === previewVersion) $("preview-loading").hidden = true;
  }
}

async function openPreview(job) {
  releasePreview();
  preview = { kind: job.kind, index: 0, files: job.files.filter((file) => ["video/mp4", "image/jpeg", "image/webp"].includes(file.mime)) };
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
  history.page = nextPage;
  $("previous-page").disabled = true;
  $("next-page").disabled = true;
  refreshJobs();
}

$("close-preview").addEventListener("click", () => $("preview-dialog").close());
$("preview-dialog").addEventListener("close", releasePreview);
$("preview-dialog").addEventListener("keydown", (event) => {
  if (preview?.kind === "gallery" && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    moveImage(event.key === "ArrowLeft" ? -1 : 1);
  }
});
$("previous-image").addEventListener("click", () => moveImage(-1));
$("next-image").addEventListener("click", () => moveImage(1));
for (const id of ["preview-video", "preview-image"]) $(id).addEventListener("error", () => {
  if (preview) notice("preview-error", remoteMode ? "无法预览此文件，请尝试下载文件。" : "无法预览此文件，请尝试打开保存文件夹或另存文件。");
});

$("collection-search").addEventListener("input", () => {
  history.q = $("collection-search").value.trim(); history.page = 1;
  ++requestVersion;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refreshJobs, 250);
});
$("collection-status").addEventListener("change", () => {
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
    await refreshJobs();
    notice("submit-success", `已清理 ${result.removed} 条失败记录，已保存的文件保留在${remoteMode ? "服务器" : "电脑"}上。`);
  } catch (error) { notice("action-error", error.message); }
  finally { $("clear-failed").disabled = false; }
});
$("video-limit").addEventListener("input", updateLimit);
$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("submit-button").disabled = true;
  notice("submit-error", ""); notice("submit-success", "");
  try {
    await api("/api/jobs", { input: $("work-input").value.trim(), video_limit_mib: selectedLimit() });
    $("work-input").value = "";
    await addJob();
  } catch (error) { notice("submit-error", error.message); }
  finally { $("submit-button").disabled = false; }
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
