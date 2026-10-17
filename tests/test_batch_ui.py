"""Batch controls against real HTTP/storage; worker claims are explicit for stable queue races."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from lumabox.web.app import create_app
from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from test_web_ui import browser, submit
from web_helpers import URL, fixture_worker


def work_url(number):
    return f"https://www.douyin.com/video/{7686430330920075200 + number}"


@pytest.fixture
def batch_server(tmp_path, media_fixtures, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = WebConfig(tmp_path / "web", port=port)
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0])
    # Only automatic claims are paused. Real API, persistence, and run_once remain intact.
    monkeypatch.setattr(worker, "start", lambda: None)
    app = create_app(config, worker=worker)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.03)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", worker
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_batch_submission_reports_duplicates_invalid_and_keeps_unaccepted_links(batch_server, browser):
    # Catches sending a multi-link paste to the single-link endpoint or discarding invalid links.
    url, worker = batch_server
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    try:
        page.goto(url)
        text = f"分享作品 {work_url(1)}\n重复 {work_url(1)}\nhttps://example.com/nope"
        page.get_by_label("作品链接或分享文案").fill(text)
        expect(page.locator("#input-count")).to_contain_text("3")
        page.get_by_role("button", name="加入队列（3）", exact=True).click()
        report = page.get_by_role("region", name="批量处理结果")
        expect(report.locator(".batch-result")).to_have_count(3)
        expect(report.get_by_text("已加入队列", exact=True)).to_be_visible()
        expect(report.get_by_text("已在队列", exact=True)).to_be_visible()
        expect(report.get_by_text("链接无效", exact=True)).to_be_visible()
        expect(page.get_by_label("作品链接或分享文案")).to_have_value("https://example.com/nope")
        assert len(worker.store.list()) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_queue_capacity_partial_acceptance_retains_unaccepted_url(batch_server, browser):
    # Catches a success notice hiding the tail of a paste when the queue fills partway through.
    url, worker = batch_server
    for index in range(9):
        worker.store.create(work_url(index))
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        page.get_by_label("作品链接或分享文案").fill(work_url(20) + "\n" + work_url(21))
        page.get_by_role("button", name="加入队列（2）", exact=True).click()
        report = page.get_by_role("region", name="批量处理结果")
        expect(report.get_by_text("队列已满", exact=True)).to_be_visible()
        expect(report.get_by_text("已加入队列", exact=True)).to_be_visible()
        expect(page.get_by_label("作品链接或分享文案")).to_have_value(work_url(21))
        assert len(worker.store.active()) == 10
    finally:
        context.close()


def test_edits_during_submission_are_preserved_and_unaccepted_links_can_be_appended(batch_server, browser):
    # Catches overwriting a new draft, or falsely saying the rejected URLs remain in that draft.
    url, _ = batch_server
    context = browser.new_context()
    page = context.new_page()
    held = []
    try:
        page.goto(url)
        page.route("**/api/jobs/batch", lambda route: held.append(route))
        page.get_by_label("作品链接或分享文案").fill(work_url(1) + "\nhttps://example.com/nope")
        with page.expect_request(lambda request: request.url == url + "/api/jobs/batch"):
            page.get_by_role("button", name="加入队列（2）", exact=True).click()
        new_text = "正在编辑另一条作品 " + work_url(25)
        page.get_by_label("作品链接或分享文案").fill(new_text)
        held.pop().continue_()
        report = page.get_by_role("region", name="批量处理结果")
        expect(report.get_by_text("链接无效", exact=True)).to_be_visible()
        expect(page.get_by_label("作品链接或分享文案")).to_have_value(new_text)
        report.get_by_role("button", name="回填未接收链接", exact=True).click()
        expect(page.get_by_label("作品链接或分享文案")).to_have_value(new_text + "\nhttps://example.com/nope")
    finally:
        while held:
            held.pop().abort()
        context.close()


@pytest.mark.parametrize("operation", ["cancel", "retry"])
def test_queue_operations_keep_unaccepted_links_from_an_edited_submission(batch_server, browser, operation):
    # Catches an operation report replacing the only recovery copy of a partially rejected paste.
    url, worker = batch_server
    failed = worker.store.create(work_url(30))
    worker.store.fail(failed["id"], "AUTH_REQUIRED")
    pending = [worker.store.create(work_url(index)) for index in range(9)]
    context = browser.new_context()
    page = context.new_page()
    held = []
    try:
        page.goto(url)
        page.route("**/api/jobs/batch", lambda route: held.append(route))
        page.get_by_label("作品链接或分享文案").fill(work_url(20) + "\n" + work_url(21) + "\nhttps://example.com/nope")
        with page.expect_request(lambda request: request.url == url + "/api/jobs/batch"):
            page.get_by_role("button", name="加入队列（3）", exact=True).click()
        new_text = "正在编辑另一条作品 " + work_url(25)
        page.get_by_label("作品链接或分享文案").fill(new_text)
        held.pop().continue_()
        report = page.get_by_role("region", name="批量处理结果")
        expect(report.get_by_text("队列已满", exact=True)).to_be_visible()
        expect(report.get_by_text("链接无效", exact=True)).to_be_visible()
        expect(page.get_by_label("作品链接或分享文案")).to_have_value(new_text)
        section = page.locator("#active-selection" if operation == "cancel" else "#history-selection")
        section.locator("summary").click()
        selected_id = pending[0]["id"] if operation == "cancel" else failed["id"]
        page.locator(f'[data-job-id="{selected_id}"] input[type=checkbox]').check()
        section.get_by_role("button", name="取消选中待下载（1）" if operation == "cancel" else "重试选中未完成（1）", exact=True).click()
        expect(page.locator("#batch-operation-notice")).to_contain_text("已取消 1" if operation == "cancel" else "已加入队列 0")
        refill = report.get_by_role("button", name="回填未接收链接", exact=True)
        expect(refill).to_be_visible()
        refill.click()
        expect(page.get_by_label("作品链接或分享文案")).to_have_value(new_text + "\n" + work_url(21) + "\nhttps://example.com/nope")
    finally:
        while held:
            held.pop().abort()
        context.close()


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_selection_survives_poll_and_cancels_only_pending_jobs(batch_server, browser):
    # Catches rerenders dropping selection/focus or letting batch cancel touch the running job.
    url, worker = batch_server
    running = worker.store.create(work_url(1))
    worker.store.claim()
    pending = worker.store.create(work_url(2))
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    try:
        page.goto(url)
        controls = page.locator("#active-selection")
        assert not controls.evaluate("node => node.open")
        controls.locator("summary").click()
        expect(page.locator(f'[data-job-id="{running["id"]}"] input[type=checkbox]')).to_have_count(0)
        checkbox = page.locator(f'[data-job-id="{pending["id"]}"] input[type=checkbox]')
        checkbox.check()
        checkbox.focus()
        # A real API change forces the poll to update the active list.
        with worker.store.connection() as db:
            db.execute("UPDATE jobs SET title=? WHERE id=?", ("轮询更新后的标题", pending["id"]))
        worker.store.create(work_url(3))
        expect(page.locator("#active-list .job-card")).to_have_count(3, timeout=6000)
        expect(page.get_by_role("heading", name="轮询更新后的标题", exact=True)).to_be_visible()
        expect(checkbox).to_be_checked()
        expect(checkbox).to_be_focused()
        assert controls.evaluate("node => node.open")
        controls.get_by_role("button", name="取消选中待下载（1）", exact=True).click()
        expect(page.locator("#batch-operation-notice")).to_contain_text("已取消 1")
        assert worker.store.get(pending["id"])["status"] == "cancelled"
        assert worker.store.get(running["id"])["status"] == "running"
        assert len(worker.store.active()) == 2
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_bulk_retry_keeps_original_options_and_history_search_clears_selection(batch_server, browser):
    # Catches using current form options for bulk retries or applying selections from another view.
    url, worker = batch_server
    first = worker.store.create(work_url(1), 37, include_audio=True)
    worker.store.fail(first["id"], "AUTH_REQUIRED")
    second = worker.store.create(work_url(2), 82, include_audio=False)
    worker.store.fail(second["id"], "AUTH_REQUIRED")
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        controls = page.locator("#history-selection")
        controls.locator("summary").click()
        first_box = page.locator(f'[data-job-id="{first["id"]}"] input[type=checkbox]')
        first_box.check()
        page.get_by_label("搜索收藏").fill("0075201")
        expect(first_box).not_to_be_checked()
        expect(controls.get_by_role("button", name="重试选中未完成（0）", exact=True)).to_be_disabled()
        page.get_by_label("搜索收藏").fill("")
        expect(page.locator("#job-list .job-card")).to_have_count(2)
        first_box.check()
        page.locator(f'[data-job-id="{second["id"]}"] input[type=checkbox]').check()
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("单视频上限").fill("4096")
        controls.get_by_role("button", name="重试选中未完成（2）", exact=True).click()
        expect(page.locator("#batch-operation-notice")).to_contain_text("已加入队列 2")
        retry_jobs = {job["source_url"]: job for job in worker.store.active()}
        assert retry_jobs[work_url(1)]["video_limit_mib"] == 37
        assert retry_jobs[work_url(1)]["include_audio"] is True
        assert retry_jobs[work_url(2)]["video_limit_mib"] == 82
        assert retry_jobs[work_url(2)]["include_audio"] is False
    finally:
        context.close()


def test_saved_batch_result_restores_only_when_requested_without_changing_audio(batch_server, browser):
    # Catches silently restoring/altering saved media from a batch whose audio checkbox is checked.
    url, worker = batch_server
    saved = worker.store.create(URL)
    assert worker.run_once()
    assert worker.store.get(saved["id"])["status"] == "success"
    worker.store.remove(saved["id"])
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("同时保存音频").check()
        page.get_by_label("作品链接或分享文案").fill(URL + "\n" + work_url(1))
        page.get_by_role("button", name="加入队列（2）", exact=True).click()
        report = page.get_by_role("region", name="批量处理结果")
        expect(report.get_by_text("已收藏", exact=True)).to_be_visible()
        with worker.store.connection() as db:
            assert db.execute("SELECT hidden_at FROM jobs WHERE id=?", (saved["id"],)).fetchone()[0] is not None
        report.get_by_role("button", name="恢复并查看", exact=True).click()
        dialog = page.locator("#existing-dialog")
        expect(dialog).to_be_visible()
        dialog.get_by_text("更多", exact=True).click()
        expect(dialog.get_by_role("button", name="提取音频", exact=True)).to_be_visible()
        assert worker.store.get(saved["id"])["audio"]["status"] == "none"
        assert worker.store.get(saved["id"])["include_audio"] is False
    finally:
        context.close()


def test_selection_limit_is_shared_and_started_item_is_skipped_on_poll(batch_server, browser):
    # Catches selecting more than 10 across regions or retaining a pending selection after claim.
    url, worker = batch_server
    failed = worker.store.create(work_url(20))
    worker.store.fail(failed["id"], "AUTH_REQUIRED")
    pending = [worker.store.create(work_url(index)) for index in range(10)]
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        active = page.locator("#active-selection")
        active.locator("summary").click()
        active.get_by_role("button", name="选择待下载", exact=True).click()
        expect(page.locator("#active-list input:checked")).to_have_count(10)
        history = page.locator("#history-selection")
        history.locator("summary").click()
        checkbox = page.locator(f'[data-job-id="{failed["id"]}"] input[type=checkbox]')
        checkbox.click()
        expect(checkbox).not_to_be_checked()
        expect(page.locator("#batch-operation-notice")).to_contain_text("10")
        claimed = worker.store.claim()
        assert claimed["id"] == pending[0]["id"]
        expect(page.locator("#active-list input:checked")).to_have_count(9, timeout=6000)
        expect(page.locator("#batch-operation-notice")).to_contain_text("跳过")
        checkbox.check()
        expect(checkbox).to_be_checked()
        expect(history.locator(".selection-count")).to_contain_text("10 / 10")
    finally:
        context.close()


def test_single_link_keeps_legacy_submit_and_more_than_ten_are_rejected(batch_server, browser):
    # Catches breaking the established single-link action or truncating an oversized batch.
    url, worker = batch_server
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        with page.expect_response(lambda response: response.url == url + "/api/jobs" and response.request.method == "POST") as response:
            submit(page, "分享 " + URL)
        assert response.value.status == 202
        expect(page.get_by_label("作品链接或分享文案")).to_have_value("")
        text = "\n".join(work_url(index) for index in range(11))
        page.get_by_label("作品链接或分享文案").fill(text)
        page.get_by_role("button", name="加入队列（11）", exact=True).click()
        expect(page.locator("#submit-error")).to_contain_text("10")
        expect(page.get_by_label("作品链接或分享文案")).to_have_value(text)
        assert len(worker.store.list()) == 1
    finally:
        context.close()
