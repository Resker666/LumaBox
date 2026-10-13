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

PASSWORD = "test-local-password-123"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def server(tmp_path, media_fixtures):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = WebConfig(tmp_path / "web", port=port)
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0], "delayed")
    app = create_app(config, PASSWORD, worker)
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


def screenshot(page, name):
    directory = Path("Downloaded/web-validation")
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / name), full_page=True)


def sign_in(page):
    page.get_by_label("访问密码").fill(PASSWORD)
    page.get_by_role("button", name="打开光匣", exact=True).click()
    expect(page.get_by_label("作品链接或分享文案")).to_be_visible()


def submit(page, text):
    page.get_by_label("作品链接或分享文案").fill(text)
    page.get_by_label("我有权下载此作品").check()
    page.get_by_role("button", name="开始下载", exact=True).click()


def test_desktop_login_poll_real_download_failure_and_logout(server, browser, media_fixtures, tmp_path):
    url, worker = server
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(url)
        screenshot(page, "login-desktop.png")
        sign_in(page)
        submit(page, '<img src=x onerror="window.injected=true"> 分享 ' + URL)
        expect(page.get_by_text("下载中", exact=True)).to_be_visible(timeout=10000)
        expect(page.get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        with page.expect_download() as download:
            page.get_by_role("link", name="下载视频").click()
        target = tmp_path / "download.mp4"
        download.value.save_as(target)
        assert target.read_bytes() == media_fixtures[0].read_bytes()
        assert page.evaluate("Boolean(window.injected)") is False
        worker.mode = "failed"
        submit(page, URL)
        expect(page.get_by_text("此作品需要登录后访问，暂时无法下载。", exact=True)).to_be_visible(timeout=10000)
        screenshot(page, "tasks-desktop.png")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.get_by_role("button", name="退出登录").click()
        expect(page.get_by_label("访问密码")).to_be_visible()
        assert page.request.get(url + "/api/jobs").status == 401
        assert not errors
    finally:
        context.close()


def test_mobile_gallery_zip_and_form_error(server, browser, media_fixtures, tmp_path):
    url, worker = server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, accept_downloads=True)
    page = context.new_page()
    try:
        page.goto(url)
        sign_in(page)
        submit(page, "不是链接")
        expect(page.get_by_text("请粘贴一条抖音视频或图集链接。", exact=True)).to_be_visible()
        submit(page, "https://v.douyin.com/test/")
        expect(page.get_by_role("link", name="下载整组 ZIP")).to_be_visible(timeout=10000)
        with page.expect_download() as download:
            page.get_by_role("link", name="下载整组 ZIP").click()
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
