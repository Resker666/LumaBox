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
  SIZE_LIMIT: "作品超出本地下载大小限制。",
  STORAGE_FULL: "存储空间不足，请清理空间或等待旧文件过期。",
  DEPENDENCY_MISSING: "下载组件尚未安装完整，请检查安装说明。",
  INTERRUPTED: "任务已中断，可以重新提交。",
  DOWNLOAD_FAILED: "下载未完成，请检查网络后重试。"
};
let csrf = null, authenticated = false, timer = null, polling = false, generation = 0, jobs = [], rendered = "";

function notice(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

function showLogin(message = "") {
  authenticated = false;
  csrf = null;
  generation += 1;
  clearTimeout(timer);
  jobs = [];
  rendered = "";
  $("job-list").replaceChildren();
  $("workspace-view").hidden = true;
  $("logout").hidden = true;
  $("login-view").hidden = false;
  notice("login-error", message);
}

async function api(path, body) {
  const options = { credentials: "same-origin", cache: "no-store", headers: {}, signal: AbortSignal.timeout(12000) };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    if (csrf) options.headers["X-CSRF-Token"] = csrf;
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin("登录已过期，请重新打开光匣。");
    throw new Error(data.message || "暂时无法完成请求，请稍后重试。");
  }
  return data;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function size(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
    top.append(element("h3", "job-title", `${gallery ? "图集" : job.kind === "video" ? "视频" : "作品"} · ${job.work_id || "正在识别"}`));
    top.append(element("span", `job-status ${job.status}`, labels[job.status] || "等待更新"));
    body.append(top);
    const date = new Date(job.created_at * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    body.append(element("p", "job-meta", `${date} 添加`));
    let detail = "";
    if (job.status === "running") detail = "正在读取作品、下载并检查文件，请稍候。";
    if (job.status === "pending") detail = "已加入队列，稍后开始。";
    if (job.status === "expired") detail = "文件已超过保留时间，可以重新提交作品链接。";
    if (job.status === "failed") detail = failures[job.error] || failures.DOWNLOAD_FAILED;
    if (job.status === "success") {
      const media = job.files.filter((file) => file.mime !== "application/zip");
      detail = `${gallery ? `${job.actual_count} 张图片` : "完整视频"} · ${size(media.reduce((sum, file) => sum + file.bytes, 0))} · 文件检查通过`;
    }
    if (detail) body.append(element("p", `job-detail ${job.status}`, detail));
    if (job.status === "success") {
      const links = element("div", "file-links");
      const ordered = [...job.files].sort((a, b) => (b.mime === "application/zip") - (a.mime === "application/zip"));
      for (const file of ordered) {
        const zip = file.mime === "application/zip";
        const name = zip ? "下载整组 ZIP" : gallery ? `图片 ${file.name.split(".")[0]}` : "下载视频";
        const link = element("a", `download-link ${zip || !gallery ? "primary" : ""}`, name);
        link.href = `/api/files/${encodeURIComponent(file.id)}`;
        link.download = file.name;
        links.append(link);
      }
      body.append(links);
    }
    card.append(body);
    fragment.append(card);
  }
  $("job-list").replaceChildren(fragment);
}

async function poll() {
  if (!authenticated || polling) return;
  polling = true;
  const current = generation;
  try {
    const data = await api("/api/jobs");
    if (authenticated && current === generation) {
      jobs = data.jobs;
      renderJobs();
      notice("connection-error", "");
    }
  } catch (error) {
    if (authenticated && current === generation) notice("connection-error", "连接暂时中断，正在尝试重新连接…");
  } finally {
    polling = false;
    if (authenticated) timer = setTimeout(poll, 2000);
  }
}

function openWorkspace(token) {
  clearTimeout(timer);
  generation += 1;
  csrf = token;
  authenticated = true;
  $("password").value = "";
  $("login-view").hidden = true;
  $("workspace-view").hidden = false;
  $("logout").hidden = false;
  notice("submit-error", "");
  notice("submit-success", "");
  poll();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-button").disabled = true;
  notice("login-error", "");
  try {
    const data = await api("/api/login", { password: $("password").value });
    openWorkspace(data.csrf);
  } catch (error) { notice("login-error", error.message || "无法连接光匣，请确认服务已启动。"); }
  finally { $("login-button").disabled = false; }
});

$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!$("authorized").checked) return;
  $("submit-button").disabled = true;
  notice("submit-error", "");
  notice("submit-success", "");
  const current = generation;
  try {
    const job = await api("/api/jobs", { input: $("work-input").value.trim(), authorized: true });
    if (authenticated && current === generation) {
      jobs = [job, ...jobs.filter((item) => item.id !== job.id)].slice(0, 30);
      renderJobs();
      $("work-input").value = "";
      $("authorized").checked = false;
      notice("submit-success", "已加入光匣，完成后即可领取。");
    }
  } catch (error) { if (authenticated) notice("submit-error", error.message); }
  finally { $("submit-button").disabled = false; }
});

$("logout").addEventListener("click", async () => {
  $("logout").disabled = true;
  try { await api("/api/logout", {}); showLogin(); }
  catch (error) { if (authenticated) notice("connection-error", error.message); }
  finally { $("logout").disabled = false; }
});

(async () => {
  try { const data = await api("/api/session"); openWorkspace(data.csrf); }
  catch { showLogin(); }
})();
