"use strict";
window.LumaBoxStorage = {
  init({ request, formatSize, onChanged }) {
    const get = (id) => document.getElementById(id);
    const dialog = get("storage-dialog");
    let page = 1, pages = 1, version = 0, signature = "";
    const active = new Set(["pending", "running", "retry_wait", "cancelling"]);
    function node(tag, className, text) {
      const result = document.createElement(tag);
      result.className = className;
      if (text !== undefined) result.textContent = text;
      return result;
    }
    function notice(id, text) { get(id).textContent = text || ""; get(id).hidden = !text; }
    function button(text, perform, destructive = false) {
      const control = node("button", destructive ? "delete-files-button" : "page-button", text);
      control.type = "button";
      control.addEventListener("click", async () => {
        control.disabled = true; notice("storage-error", ""); notice("storage-success", "");
        try { await perform(); }
        catch (error) { notice("storage-error", error.message); }
        finally { control.disabled = false; }
      });
      return control;
    }
    async function changed() { await onChanged(); signature = ""; await refresh(); }
    function card(item) {
      const body = node("article", "storage-item");
      body.dataset.storageJobId = item.id;
      const title = item.title || `${item.kind === "gallery" ? "图集" : "作品"} · ${item.work_id || "未识别作品"}`;
      body.append(node("h3", "job-title", title));
      const date = new Date(item.created_at * 1000).toLocaleString("zh-CN");
      body.append(node("p", "job-meta", `${date} · ${item.file_count} 个文件 · ${formatSize(item.bytes)}（含打包与报告）`));
      const deleting = ["deleting", "delete_failed"].includes(item.storage_state);
      const busy = active.has(item.status) || item.audio_active;
      const state = item.audio_active ? "音频正在处理，完成或取消后可管理" : active.has(item.status) ? "任务正在处理，暂不能删除" : deleting ? "文件尚未全部删除" : item.hidden ? "记录已移除，文件保留" : "文件和收藏记录均保留";
      body.append(node("p", "job-detail", state));
      const controls = node("div", "job-controls");
      if (item.hidden && item.status === "success" && !deleting && !busy) controls.append(button("恢复收藏", async () => {
        await request(`/api/jobs/${encodeURIComponent(item.id)}/restore`, {});
        await changed(); notice("storage-success", "已恢复到收藏。");
      }));
      if (!busy) {
        if (!item.hidden && !deleting) controls.append(button("仅移除记录", async () => {
          if (!window.confirm(`仅移除“${title}”的记录？文件会保留，仍可在文件管理中找到。`)) return;
          await request(`/api/jobs/${encodeURIComponent(item.id)}/remove`, {});
          await changed(); notice("storage-success", "记录已移除，文件仍保留。");
        }));
        controls.append(button(deleting ? "重试删除" : "删除记录及文件", async () => {
          const identity = item.work_id ? `\n作品 ID：${item.work_id}` : "";
          if (!window.confirm(`删除“${title}”的记录及文件？${identity}\n${item.file_count} 个文件，${formatSize(item.bytes)}。\n删除后无法从光匣恢复。`)) return;
          const result = await request(`/api/jobs/${encodeURIComponent(item.id)}/purge`, { confirm: true });
          await changed();
          if (result.status !== "deleted") throw new Error(result.message || "文件尚未全部删除，请重试。");
          notice("storage-success", `已删除文件大小 ${formatSize(result.deleted_bytes)}，空间信息已刷新。`);
        }, true));
      }
      body.append(controls); return body;
    }
    async function refresh(force = false) {
      const current = ++version;
      try {
        const summary = await request(`/api/storage${force ? "?refresh=1" : ""}`);
        if (current !== version) return;
        get("storage-total").textContent = formatSize(summary.output_bytes);
        get("storage-free").textContent = summary.disk_free_bytes === null ? "暂不可用" : formatSize(summary.disk_free_bytes);
        get("storage-updated").textContent = `更新于 ${new Date(summary.sampled_at * 1000).toLocaleTimeString("zh-CN")}`;
        notice("storage-health", summary.complete ? "" : "统计不完整，部分文件暂时无法读取。");
        get("storage-summary").textContent = `${summary.saved_count} 份已保存作品 · 媒体及打包 ${formatSize(summary.saved_bytes)} · 其中隐藏收藏 ${formatSize(summary.hidden_bytes)} · 其他/临时 ${formatSize(summary.other_bytes)}`;
        if (!dialog.open) return;
        const result = await request(`/api/storage/items?scope=${encodeURIComponent(get("storage-scope").value)}&page=${page}`);
        if (current !== version) return;
        pages = Math.max(1, Math.ceil(result.total / result.page_size));
        if (page > pages) { page = pages; return refresh(); }
        const nextSignature = JSON.stringify(result);
        if (nextSignature !== signature) {
          const focused = document.activeElement;
          const focusJob = focused?.closest("[data-storage-job-id]")?.dataset.storageJobId;
          const focusText = focused?.textContent;
          get("storage-items").replaceChildren(...result.items.map(card));
          if (focusJob) [...get("storage-items").querySelectorAll("button")].find((item) => item.closest("[data-storage-job-id]").dataset.storageJobId === focusJob && item.textContent === focusText)?.focus({preventScroll: true});
          signature = nextSignature;
        }
        get("storage-empty").hidden = result.items.length !== 0;
        get("storage-page").textContent = `第 ${page} / ${pages} 页`;
        get("storage-previous").disabled = page <= 1;
        get("storage-next").disabled = page >= pages;
      } catch (error) { if (current === version) notice("storage-error", error.message); }
    }
    get("open-storage").addEventListener("click", () => {
      notice("storage-error", ""); notice("storage-success", ""); dialog.showModal(); refresh(true);
    });
    get("close-storage").addEventListener("click", () => dialog.close());
    get("storage-scope").addEventListener("change", () => { page = 1; refresh(); });
    get("storage-refresh").addEventListener("click", () => refresh(true));
    get("storage-previous").addEventListener("click", () => { if (page > 1) { --page; refresh(); } });
    get("storage-next").addEventListener("click", () => { if (page < pages) { ++page; refresh(); } });
    window.addEventListener("lumabox:storage-changed", () => refresh());
    setInterval(() => refresh(), 30000);
    refresh();
    return { refresh, invalidate: () => refresh() };
  }
};
