import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from lumabox.web.app import create_app
from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from web_helpers import URL, fixture_worker

@pytest.fixture(scope="module")
def browser(request):
    with sync_playwright() as p:
        engine_name = getattr(request, "param", "chromium")
        engine = getattr(p, engine_name)
        if engine_name == "webkit" and not Path(engine.executable_path).is_file():
            pytest.skip("Install Playwright WebKit to run the Safari compatibility check")
        browser = engine.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def server(tmp_path, media_fixtures, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = WebConfig(tmp_path / "web", port=port)
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0], "delayed")
    import lumabox.web.app as web_app
    opened = []
    monkeypatch.setattr(web_app, "open_folder", lambda path: opened.append(path))
    app = create_app(config, worker=worker)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.03)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", worker, opened
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
        assert not thread.is_alive()


def screenshot(page, name):
    directory = Path("Downloaded/local-ux-validation")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name), full_page=True)


def submit(page, text):
    page.get_by_label("作品链接或分享文案").fill(text)
    page.get_by_role("button", name="开始下载", exact=True).click()


def test_desktop_direct_use_limits_save_folder_download_and_retry(server, browser, media_fixtures, tmp_path):
    url, worker, opened = server
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(5000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(url)
        expect(page.get_by_label("作品链接或分享文案")).to_be_visible()
        expect(page.get_by_label("访问密码")).to_have_count(0)
        expect(page.get_by_label("我有权下载此作品")).to_have_count(0)
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("单视频上限").fill("2048")
        submit(page, '<img src=x onerror="window.injected=true"> 分享 ' + URL)
        expect(page.locator("#active-list").get_by_text("下载中", exact=True)).to_be_visible(timeout=10000)
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        page.get_by_text("更多", exact=True).click()
        page.get_by_role("button", name="打开文件夹", exact=True).click()
        expect(page.get_by_text("已打开保存文件夹。", exact=True)).to_be_visible()
        assert len(opened) == 1 and opened[0].is_relative_to(worker.config.output_dir)
        page.get_by_text("另存文件", exact=True).click()
        with page.expect_download() as download:
            page.get_by_role("link", name="另存视频").click()
        target = tmp_path / "download.mp4"
        download.value.save_as(target)
        assert target.read_bytes() == media_fixtures[0].read_bytes()
        assert page.evaluate("Boolean(window.injected)") is False
        worker.mode = "failed"
        submit(page, "https://v.douyin.com/test/")
        expect(page.get_by_text("此作品需要登录后访问，暂时无法下载。", exact=True)).to_be_visible(timeout=10000)
        expect(page.get_by_text("作品 · 未识别作品", exact=True)).to_be_visible()
        worker.mode = "success"
        page.get_by_role("button", name="一键重试", exact=True).click()
        expect(page.locator("#job-list").get_by_text("已有收藏", exact=True)).to_have_count(1, timeout=10000)
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_have_count(1)
        screenshot(page, "tasks-desktop.png")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        jobs = page.request.get(url + "/api/jobs").json()["jobs"]
        assert jobs[0]["video_limit_mib"] == 2048
        page.reload()
        expect(page.get_by_label("单视频上限")).to_have_value("2048")
        assert not errors
    finally:
        context.close()


def test_mobile_gallery_zip_and_form_error(server, browser, media_fixtures, tmp_path):
    url, worker, opened = server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(5000)
    try:
        page.goto(url)
        expect(page.get_by_label("作品链接或分享文案")).to_be_visible()
        submit(page, "不是链接")
        expect(page.get_by_text("请粘贴一条抖音视频或图集链接。", exact=True)).to_be_visible()
        submit(page, "https://v.douyin.com/test/")
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        page.get_by_text("更多", exact=True).click()
        expect(page.get_by_role("button", name="打开文件夹", exact=True)).to_be_visible()
        page.get_by_text("另存文件", exact=True).click()
        with page.expect_download() as download:
            page.get_by_role("link", name="另存整组 ZIP").click()
        target = tmp_path / "images.zip"
        download.value.save_as(target)
        with zipfile.ZipFile(target) as archive:
            assert archive.read("01.jpg") == media_fixtures[1].read_bytes()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        screenshot(page, "tasks-mobile.png")
    finally:
        context.close()


def test_launcher_help_exposes_only_local_options():
    result = subprocess.run([sys.executable, "-m", "lumabox.web", "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "--port" in result.stdout and "--headless" in result.stdout
    assert "--host" not in result.stdout and "--password" not in result.stdout


def test_size_error_shows_actual_size_and_task_limit(server, browser):
    url, worker, _ = server
    worker.mode = "size_failed"
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        submit(page, URL)
        expect(page.get_by_text("文件大小为 1.5 GiB，超过此任务的 1 GiB 上限。", exact=True)).to_be_visible(timeout=10000)
        expect(page.get_by_role("button", name="一键重试", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("detail_key", ["actual_bytes", "at_least_bytes"])
def test_size_error_one_byte_above_limit_shows_distinct_exact_sizes(server, browser, detail_key):
    url, worker, _ = server
    job = worker.store.create(URL)
    worker.store.fail(job["id"], "SIZE_LIMIT", {"diagnostics": {
        "limit_bytes": 1073741824, detail_key: 1073741825}})
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_text("1,073,741,825 字节", exact=False)).to_be_visible(timeout=5000)
        expect(page.get_by_text("1,073,741,824 字节", exact=False)).to_be_visible()
    finally:
        context.close()


def test_polling_connection_notice_clears_after_recovery(server, browser):
    url, _, _ = server
    context = browser.new_context()
    page = context.new_page()
    try:
        page.route("**/api/jobs?*", lambda route: route.abort())
        page.goto(url)
        warning = page.get_by_text("连接暂时中断，正在尝试重新连接…", exact=True)
        expect(warning).to_be_visible(timeout=5000)
        page.unroute("**/api/jobs?*")
        expect(warning).to_be_hidden(timeout=5000)
    finally:
        context.close()
