"""Daily-use UI exercised against real local HTTP, worker media and Chromium."""
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect

from lumabox.web.app import create_app
from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from test_web_ui import browser, submit
from web_helpers import URL, WORK_ID, fixture_worker


@pytest.fixture
def daily_server(tmp_path, media_fixtures, monkeypatch, request):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = WebConfig(tmp_path / "web", port=port, remote_mode=getattr(request, "param", False))
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0])
    import lumabox.web.app as web_app
    monkeypatch.setattr(web_app, "open_folder", lambda path: None)
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


def failed_job(store, work_id):
    job = store.create(f"https://www.douyin.com/video/{work_id}")
    store.fail(job["id"], "AUTH_REQUIRED")
    return job


def wait_finished(store, job_id):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job["status"] in ("success", "failed"):
            assert job["status"] == "success", job
            return job
        time.sleep(.05)
    pytest.fail("fixture worker did not finish")


def screenshot(page, name, *, full_page=True):
    directory = Path("Downloaded/daily-use-validation")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name), full_page=full_page)


def test_history_search_status_pagination_and_submission_resets_view(daily_server, browser):
    # Catches omitted query parameters, local-only totals and a new job hidden by old filters.
    url, worker = daily_server
    for index in range(21):
        failed_job(worker.store, str(7686430330920075200 + index))
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_label("搜索收藏")).to_be_visible()
        expect(page.locator("#task-count")).to_have_text("21 份收藏")
        expect(page.locator(".job-card")).to_have_count(20)
        page.get_by_role("button", name="下一页", exact=True).click()
        expect(page.locator(".job-card")).to_have_count(1)
        expect(page.locator("#page-info")).to_have_text("第 2 / 2 页")
        page.get_by_label("搜索收藏").fill("7686430330920075205")
        expect(page.locator(".job-card")).to_have_count(1)
        expect(page.locator("#task-count")).to_have_text("1 份收藏")
        page.get_by_label("任务状态").select_option("success")
        expect(page.locator(".job-card")).to_have_count(0)
        submit(page, URL)
        expect(page.get_by_label("搜索收藏")).to_have_value("")
        expect(page.get_by_label("任务状态")).to_have_value("all")
        expect(page.locator("#page-info")).to_have_text("第 1 / 2 页")
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=12000)
        retryable = worker.store.create("https://v.douyin.com/test/")
        worker.store.fail(retryable["id"], "AUTH_REQUIRED")
        page.get_by_label("搜索收藏").fill("v.douyin.com/test")
        page.get_by_label("任务状态").select_option("failed")
        expect(page.locator(".job-card")).to_have_count(1)
        page.get_by_role("button", name="一键重试", exact=True).click()
        expect(page.get_by_label("搜索收藏")).to_have_value("")
        expect(page.get_by_label("任务状态")).to_have_value("all")
        expect(page.locator("#page-info")).to_have_text("第 1 / 2 页")
        expect(page.locator("#job-list").get_by_text("已有收藏", exact=True)).to_have_count(1, timeout=12000)
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_have_count(1)
        screenshot(page, "history-desktop.png")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_remove_record_confirmation_retains_media_and_clear_failed(daily_server, browser):
    # Catches accidental deletion of media, missing confirmation, and overly broad clearing.
    url, worker = daily_server
    job = worker.store.create(URL)
    completed = wait_finished(worker.store, job["id"])
    saved = worker.store.file_path(worker.store.file(completed["files"][0]["id"]))
    original = saved.read_bytes()
    failed_job(worker.store, "7686430330920075201")
    failed_job(worker.store, "7686430330920075202")
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        card = page.locator(".job-card").filter(has=page.get_by_text("已完成", exact=True))
        card.get_by_text("更多", exact=True).click()
        expect(card.get_by_role("button", name="移除记录", exact=True)).to_be_visible()
        confirmations = []
        def cancel(dialog):
            confirmations.append(dialog.message)
            dialog.dismiss()
        page.once("dialog", cancel)
        card.get_by_role("button", name="移除记录", exact=True).click()
        assert confirmations and "文件" in confirmations[0] and "保留" in confirmations[0]
        expect(card).to_have_count(1)
        page.get_by_role("button", name="清理失败记录", exact=True).click()
        expect(page.locator(".job-card")).to_have_count(1)
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="移除记录", exact=True).click()
        expect(page.locator(".job-card")).to_have_count(0)
        assert saved.read_bytes() == original
        assert page.request.get(url + "/api/jobs").json()["total"] == 0
    finally:
        context.close()


def test_video_preview_uses_range_seek_and_survives_polling(daily_server, browser):
    # Catches list rerenders destroying the player/details/focus or failing to release media.
    url, worker = daily_server
    job = worker.store.create(URL)
    completed = wait_finished(worker.store, job["id"])
    file_id = completed["files"][0]["id"]
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    ranges = []
    page.on("response", lambda response: ranges.append(response) if response.url.endswith("/preview") and response.request.headers.get("range") else None)
    try:
        page.goto(url)
        page.get_by_text("更多", exact=True).click()
        page.get_by_text("另存文件", exact=True).click()
        preview_button = page.get_by_role("button", name="预览视频", exact=True)
        expect(preview_button).to_be_visible()
        preview_button.focus()
        failed_job(worker.store, "7686430330920075201")
        expect(page.locator("#job-list").get_by_text("未完成", exact=True)).to_be_visible(timeout=6000)
        assert page.locator("details.save-extra").evaluate("node => node.open")
        expect(preview_button).to_be_focused()
        preview_button.click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        video = dialog.locator("video")
        expect(video).to_have_attribute("controls", "")
        page.wait_for_function("() => document.querySelector('dialog video').readyState >= 1")
        assert video.evaluate("node => node.duration") > .3
        assert any(response.status == 206 for response in ranges)
        assert ranges[0].url == url + f"/api/files/{file_id}/preview"
        video.evaluate("node => { node.currentTime = 0.2; }")
        page.wait_for_function("() => document.querySelector('dialog video').currentTime >= .19")
        failed_job(worker.store, "7686430330920075202")
        expect(page.locator("#job-list").get_by_text("未完成", exact=True)).to_have_count(2, timeout=6000)
        expect(dialog).to_be_visible()
        assert video.evaluate("node => node.currentTime") >= .19
        screenshot(page, "video-preview-desktop.png", full_page=False)
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        page.wait_for_function("() => !document.getElementById('preview-video').hasAttribute('src')", timeout=5000)
        assert page.locator("#preview-video").get_attribute("src") is None
    finally:
        context.close()


def test_gallery_preview_preserves_file_order_and_keyboard_mobile(daily_server, browser, media_fixtures):
    # Catches ZIP entering previews, lexicographic reordering and broken keyboard navigation.
    url, worker = daily_server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    child = worker.script.read_text(encoding="utf-8")
    child = child.replace("if mode == 'gallery_large':\n    for index in range(2,11):", "if mode in ('gallery','gallery_large'):\n    for index in range(2,4):")
    child = child.replace("result.update(expected_count=10,actual_count=10,bytes=10*len(data))", "result.update(expected_count=3,actual_count=3,bytes=3*len(data))")
    worker.script.write_text(child, encoding="utf-8")
    job = worker.store.create("https://v.douyin.com/test/")
    completed = wait_finished(worker.store, job["id"])
    image_ids = [item["id"] for item in completed["files"] if item["mime"] != "application/zip"]
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    try:
        page.goto(url)
        page.get_by_role("button", name="预览图集", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        image = dialog.locator("img")
        expect(image).to_have_attribute("src", f"/api/files/{image_ids[0]}/preview")
        page.wait_for_function("() => document.querySelector('dialog img').naturalWidth === 64")
        expect(dialog.get_by_text("1 / 3", exact=True)).to_be_visible()
        page.keyboard.press("ArrowRight")
        expect(image).to_have_attribute("src", f"/api/files/{image_ids[1]}/preview")
        page.keyboard.press("ArrowRight")
        expect(image).to_have_attribute("src", f"/api/files/{image_ids[2]}/preview")
        expect(dialog.get_by_role("button", name="下一张", exact=True)).to_be_disabled()
        page.keyboard.press("ArrowLeft")
        expect(dialog.get_by_text("2 / 3", exact=True)).to_be_visible()
        screenshot(page, "gallery-preview-mobile.png", full_page=False)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        dialog.get_by_role("button", name="关闭预览", exact=True).click()
        screenshot(page, "history-mobile.png")
    finally:
        context.close()


def test_missing_preview_file_gives_actionable_notice(daily_server, browser):
    # Catches falsely claiming a playable file after its bytes disappear on disk.
    url, worker = daily_server
    job = worker.store.create(URL)
    completed = wait_finished(worker.store, job["id"])
    worker.store.file_path(worker.store.file(completed["files"][0]["id"])).unlink()
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        page.get_by_role("button", name="预览视频", exact=True).click()
        expect(page.get_by_role("dialog").get_by_text("文件可能已移动或删除，请检查保存位置后重新下载。", exact=True)).to_be_visible()
    finally:
        context.close()


def test_title_and_real_progress_are_rendered_without_html(daily_server, browser):
    # Catches unsafe title insertion, invented percentages and ignored phase/byte snapshots.
    url, worker = daily_server
    worker.mode = "delayed"
    worker.script.write_text(worker.script.read_text(encoding="utf-8").replace("time.sleep(3)", "time.sleep(10)"), encoding="utf-8")
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        submit(page, URL)
        expect(page.locator("#active-list").get_by_text("下载中", exact=True)).to_be_visible(timeout=6000)
        job = worker.store.list()[0]
        title = '<img src=x onerror="window.injected=true"> 我的收藏'
        assert worker.store.update_progress(job["id"], {"stage": "media_transfer", "work_id": WORK_ID, "kind": "video", "title": title, "bytes_received": 1048576, "bytes_total": 4194304})
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible(timeout=6000)
        expect(page.get_by_text("1 MiB / 4 MiB · 25%", exact=False)).to_be_visible()
        assert not page.evaluate("Boolean(window.injected)")
        assert worker.store.update_progress(job["id"], {"stage": "media_transfer", "bytes_received": 1048576, "bytes_total": None})
        expect(page.get_by_text("已接收 1 MiB", exact=False)).to_be_visible(timeout=6000)
        expect(page.locator(".job-progress-meter")).to_have_count(0)
        expect(page.locator(".job-detail")).not_to_contain_text("%")
        assert worker.store.update_progress(job["id"], {"stage": "media_verification", "bytes_received": 1048576, "bytes_total": None})
        expect(page.get_by_text("检查文件", exact=False)).to_be_visible(timeout=6000)
        expect(page.locator(".job-progress-meter")).to_have_count(0)
        expect(page.locator(".job-detail")).not_to_contain_text("%")
        expect(page.get_by_role("button", name="移除记录", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_gallery_progress_uses_verified_counts_and_current_image(daily_server, browser, media_fixtures):
    # Catches converting unknown aggregate gallery bytes into a invented percentage.
    url, worker = daily_server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    child = worker.script.read_text(encoding="utf-8").replace("if mode == 'timeout':", "if mode == 'gallery':\n    time.sleep(14)\nif mode == 'timeout':")
    worker.script.write_text(child, encoding="utf-8")
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    try:
        page.goto(url)
        submit(page, "https://v.douyin.com/test/")
        expect(page.locator("#active-list").get_by_text("下载中", exact=True)).to_be_visible(timeout=6000)
        job = worker.store.list()[0]
        assert worker.store.update_progress(job["id"], {"stage": "media_transfer", "work_id": WORK_ID, "kind": "gallery", "bytes_received": 1048576, "bytes_total": None, "completed_count": 1, "expected_count": 3, "resource_index": 2})
        expect(page.get_by_text("已检查 1 / 3 张", exact=False)).to_be_visible(timeout=6000)
        expect(page.get_by_text("正在下载第 2 张", exact=False)).to_be_visible()
        expect(page.get_by_text("已接收 1 MiB", exact=False)).to_be_visible()
        expect(page.locator(".job-progress-meter")).to_have_count(0)
        expect(page.locator(".job-detail")).not_to_contain_text("%")
        assert worker.store.update_progress(job["id"], {"stage": "media_transfer", "bytes_received": 2097152, "bytes_total": None, "completed_count": 2, "expected_count": 3, "resource_index": 3})
        expect(page.get_by_text("已检查 2 / 3 张", exact=False)).to_be_visible(timeout=6000)
        expect(page.get_by_text("正在下载第 3 张", exact=False)).to_be_visible()
        screenshot(page, "progress-mobile.png")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_poll_recovery_retains_an_unrelated_action_error(daily_server, browser):
    # Catches a successful list poll erasing a failed folder-open notice.
    url, worker = daily_server
    job = worker.store.create(URL)
    completed = wait_finished(worker.store, job["id"])
    worker.store.file_path(worker.store.file(completed["files"][0]["id"])).unlink()
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        page.get_by_text("更多", exact=True).click()
        page.get_by_role("button", name="打开文件夹", exact=True).click()
        error = page.locator("#action-error")
        expect(error).to_be_visible()
        message = error.inner_text()
        page.route("**/api/jobs?*", lambda route: route.abort())
        expect(page.locator("#connection-error")).to_be_visible(timeout=6000)
        page.unroute("**/api/jobs?*")
        expect(page.locator("#connection-error")).to_be_hidden(timeout=6000)
        expect(error).to_have_text(message)
        expect(error).to_be_visible()
    finally:
        context.close()


def test_rapid_paging_cannot_send_an_invalid_page_while_loading(daily_server, browser):
    # Catches queued clicks decrementing client page below 1 before the first reply.
    url, worker = daily_server
    for index in range(21):
        failed_job(worker.store, str(7686430330920075200 + index))
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(5000)
    held, requested = [], []
    try:
        page.goto(url)
        page.get_by_role("button", name="下一页", exact=True).click()
        expect(page.locator("#page-info")).to_have_text("第 2 / 2 页")
        def delay_first_page(route):
            number = int(parse_qs(urlsplit(route.request.url).query)["page"][0])
            requested.append(number)
            if number == 1 and not held:
                held.append(route)
            else:
                route.continue_()
        page.route("**/api/jobs?*", delay_first_page)
        previous = page.get_by_role("button", name="上一页", exact=True)
        previous.dblclick()
        assert requested == [1], requested
        expect(previous).to_be_disabled(timeout=1000)
        expect(page.get_by_role("button", name="下一页", exact=True)).to_be_disabled()
        held.pop().continue_()
        expect(page.locator("#page-info")).to_have_text("第 1 / 2 页")
        expect(previous).to_be_disabled()
        expect(page.get_by_role("button", name="下一页", exact=True)).to_be_enabled()
        expect(page.locator("#connection-error")).to_be_hidden()
        assert all(number >= 1 for number in requested)
    finally:
        while held:
            held.pop().abort()
        context.close()
