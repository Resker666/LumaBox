"use strict";
const $ = (id) => document.getElementById(id);
const labels = { pending: "待下载", running: "下载中", success: "已完成", failed: "未完成", expired: "已过期" };
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
let csrf = null, timer = null, polling = false, jobs = [], rendered = "";

function notice(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

async function api(path, body) {
  const options = { cache: "no-store", headers: {}, signal: AbortSignal.timeout(12000) };
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
  return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
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

function addJob(job) {
  jobs = [job, ...jobs.filter((item) => item.id !== job.id)].slice(0, 30);
  renderJobs();
  notice("submit-success", "已加入光匣，完成后自动保存在本机。");
}

function action(text, className, perform) {
  const button = element("button", className, text);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    notice("connection-error", "");
    try { await perform(); }
    catch (error) { notice("connection-error", error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

function renderJobs() {
  const signature = JSON.stringify(jobs);
  if (signature === rendered) return;
  rendered = signature;
  $("task-count").textContent = `${jobs.length} 份收藏`;
  $("empty-state").hidden = jobs.length !== 0;
  const fragment = document.createDocumentFragment();
  for (const job of jobs) {
    const gallery = job.kind === "gallery";
    const card = element("article", "job-card");
    card.append(element("div", `job-icon ${gallery ? "gallery" : ""}`, gallery ? "▧" : "▶"));
    const body = element("div", "job-body"), top = element("div", "job-top");
    const identity = job.work_id || (job.status === "running" ? "正在识别" : job.status === "pending" ? "等待识别" : "未识别作品");
    top.append(element("h3", "job-title", `${gallery ? "图集" : job.kind === "video" ? "视频" : "作品"} · ${identity}`));
    top.append(element("span", `job-status ${job.status}`, labels[job.status] || "等待更新"));
    body.append(top);
    const date = new Date(job.created_at * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    body.append(element("p", "job-meta", `${date} 添加`));
    if (job.source_url) body.append(element("p", "job-source", job.source_url));
    let detail = "";
    if (job.status === "running") detail = "正在读取作品、下载并检查文件，请稍候。";
    if (job.status === "pending") detail = "已加入队列，稍后开始。";
    if (job.status === "expired") detail = "旧版临时文件已过期，可以重新保存此作品。";
    if (job.status === "failed") detail = failureText(job);
    if (job.status === "success") {
      const media = job.files.filter((file) => file.mime !== "application/zip");
      detail = `${gallery ? `${job.actual_count} 张图片` : "完整视频"} · ${size(media.reduce((sum, file) => sum + file.bytes, 0))} · 已保存到本机`;
    }
    if (detail) body.append(element("p", `job-detail ${job.status}`, detail));
    if (job.status === "success") {
      if (job.output_directory) body.append(element("p", "job-path", `保存位置：${job.output_directory}`));
      const controls = element("div", "job-controls");
      if (job.output_directory) controls.append(action("打开文件夹", "folder-button", async () => {
        await api(`/api/jobs/${job.id}/open-folder`, {});
        notice("submit-success", "已打开保存文件夹。");
      }));
      const extra = element("details", "save-extra");
      extra.append(element("summary", "", "另存文件"));
      const links = element("div", "file-links");
      const ordered = [...job.files].sort((a, b) => (b.mime === "application/zip") - (a.mime === "application/zip"));
      for (const file of ordered) {
        const zip = file.mime === "application/zip";
        const name = zip ? "另存整组 ZIP" : gallery ? `另存图片 ${file.name.split(".")[0]}` : "另存视频";
        const link = element("a", "download-link", name);
        link.href = `/api/files/${encodeURIComponent(file.id)}`;
        link.download = file.name;
        links.append(link);
      }
      extra.append(links);
      controls.append(extra);
      body.append(controls);
    }
    if (job.status === "failed" || job.status === "expired") {
      const controls = element("div", "job-controls");
      controls.append(action("一键重试", "retry-button", async () => {
        const retry = await api(`/api/jobs/${job.id}/retry`, { video_limit_mib: selectedLimit() });
        addJob(retry);
      }));
      body.append(controls);
    }
    card.append(body);
    fragment.append(card);
  }
  $("job-list").replaceChildren(fragment);
}

async function poll() {
  if (polling || !csrf) return;
  polling = true;
  try {
    jobs = (await api("/api/jobs")).jobs;
    renderJobs();
    if ($("connection-error").textContent === "连接暂时中断，正在尝试重新连接…") notice("connection-error", "");
  } catch { notice("connection-error", "连接暂时中断，正在尝试重新连接…"); }
  finally { polling = false; timer = setTimeout(poll, 2000); }
}

$("video-limit").addEventListener("input", updateLimit);
$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("submit-button").disabled = true;
  notice("submit-error", "");
  notice("submit-success", "");
  try {
    addJob(await api("/api/jobs", { input: $("work-input").value.trim(), video_limit_mib: selectedLimit() }));
    $("work-input").value = "";
  } catch (error) { notice("submit-error", error.message); }
  finally { $("submit-button").disabled = false; }
});

async function start() {
  try {
    const settings = await api("/api/session");
    csrf = settings.csrf;
    let remembered;
    try { remembered = Number(localStorage.getItem("lumabox.videoLimitMiB")); } catch {}
    $("video-limit").value = Number.isInteger(remembered) && remembered >= 1 && remembered <= 8192 ? remembered : settings.video_limit_mib;
    $("output-root").textContent = settings.output_directory;
    updateLimit();
    $("submit-button").disabled = false;
    notice("connection-error", "");
    poll();
  } catch {
    notice("connection-error", "暂时无法连接光匣，正在尝试重新连接…");
    timer = setTimeout(start, 3000);
  }
}
start();
