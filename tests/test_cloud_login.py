"""Cloud login protects actual routes and files; local mode stays password-free."""
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import expect

from lumabox.web.app import create_app
from lumabox.web.config import WebConfig, WebError
from lumabox.web.store import JobStore
from test_web_ui import browser, submit
from web_helpers import URL, fixture_worker

ORIGIN = "http://127.0.0.1:8765"
PASSWORD = "test-only-cloud-password"


def access_app(tmp_path, media_fixtures, *, port=8765, secure=False):
    secret = tmp_path / "access.json"
    secret.write_text(json.dumps({"username": "lumabox", "password": PASSWORD, "url": ORIGIN}))
    config = WebConfig(tmp_path / "web", port=port, remote_mode=True, access_file=secret, secure_cookie=secure)
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0])
    return create_app(config, worker=worker)


@pytest.fixture
def protected(tmp_path, media_fixtures):
    app = access_app(tmp_path, media_fixtures)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def login(client, password=PASSWORD, *, remember=True):
    return client.post("/auth/login", json={"password": password, "remember": remember}, headers={"Origin": ORIGIN})


def test_anonymous_requests_cannot_read_history_or_media(protected):
    page = protected.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert "www-authenticate" not in page.headers
    for path in ("/api/session", "/api/jobs", "/api/files/" + "a" * 32, "/api/files/" + "a" * 32 + "/preview"):
        response = protected.get(path)
        assert response.status_code == 401
        assert response.json()["error"] == "LOGIN_REQUIRED"
    assert protected.get("/login").status_code == 200
    for path in ("/static/app.css", "/static/login.css", "/static/login.js"):
        assert protected.get(path).status_code == 200
    assert protected.get("/static/app.js", follow_redirects=False).status_code == 303


def test_cookie_login_logout_and_replay_revocation(protected):
    assert login(protected, "incorrect").status_code == 401
    response = login(protected)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=604800" in cookie
    token = protected.cookies["lumabox_session"]
    assert PASSWORD not in token and len(token) >= 32
    session = protected.get("/api/session").json()
    assert session["access_protected"] is True
    assert protected.get("/login", follow_redirects=False).headers["location"] == "/"
    assert protected.post("/auth/logout", json={}, headers={"Origin": ORIGIN}).status_code == 403
    response = protected.post("/auth/logout", json={}, headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf"]})
    assert response.status_code == 200
    protected.cookies.set("lumabox_session", token)
    assert protected.get("/api/jobs").status_code == 401


def test_sessions_expire_and_failures_are_rate_limited(protected, monkeypatch):
    import lumabox.web.access as access
    clock = [100.0]
    monkeypatch.setattr(access, "monotonic", lambda: clock[0])
    assert login(protected).status_code == 200
    clock[0] += 604801
    assert protected.get("/api/session").status_code == 401
    for _ in range(5):
        assert login(protected, "wrong").status_code == 401
    limited = login(protected)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    clock[0] += 61
    assert login(protected, remember=False).status_code == 200
    assert "max-age=" not in login(protected, remember=False).headers["set-cookie"].lower()


@pytest.mark.parametrize("headers,body,status", [
    ({"Origin": "https://evil.example"}, {"password": PASSWORD}, 403),
    ({}, {"password": PASSWORD}, 403),
    ({"Origin": "null"}, {"password": PASSWORD}, 403),
    ({"Origin": ORIGIN}, {"password": 123}, 400),
    ({"Origin": ORIGIN}, {"password": PASSWORD, "remember": "yes"}, 400),
    ({"Origin": ORIGIN}, {"password": "x" * 9000}, 413),
])
def test_login_keeps_existing_request_boundaries(protected, headers, body, status):
    assert protected.post("/auth/login", headers=headers, json=body).status_code == status


def test_secure_cookie_and_invalid_credential_file(tmp_path, media_fixtures):
    with TestClient(access_app(tmp_path, media_fixtures, secure=True), base_url="https://127.0.0.1:8765") as client:
        response = login(client)
        assert "; secure" in response.headers["set-cookie"].lower()
        assert client.get("/api/session").status_code == 200
    with pytest.raises(WebError):
        create_app(WebConfig(tmp_path / "bad", access_file=tmp_path / "absent.json"))


def test_malformed_password_cannot_turn_login_into_server_error(protected):
    response = protected.post("/auth/login", content=b'{"password":"\\ud800"}', headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 400


def test_login_rotates_existing_session(protected):
    assert login(protected).status_code == 200
    previous = protected.cookies["lumabox_session"]
    assert login(protected).status_code == 200
    assert protected.cookies["lumabox_session"] != previous
    assert protected.get("/api/session", headers={"Cookie": "lumabox_session=" + previous}).status_code == 401


@pytest.fixture
def login_server(tmp_path, media_fixtures):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(access_app(tmp_path, media_fixtures, port=port), host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.03)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
        assert not thread.is_alive()


def test_login_form_remember_download_logout_and_mobile(login_server, browser, tmp_path, media_fixtures):
    context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 960})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(login_server)
        expect(page).to_have_url(login_server + "/login")
        password = page.get_by_label("访问密码", exact=True)
        password.fill("wrong")
        page.get_by_role("button", name="进入光匣", exact=True).click()
        expect(page.get_by_role("alert")).to_be_visible()
        password.fill(PASSWORD)
        page.get_by_role("button", name="显示密码", exact=True).click()
        expect(password).to_have_attribute("type", "text")
        page.get_by_role("button", name="隐藏密码", exact=True).click()
        expect(password).to_have_attribute("type", "password")
        expect(page.get_by_label("记住登录 7 天")).to_be_checked()
        directory = Path("Downloaded/login-validation")
        directory.mkdir(parents=True, exist_ok=True)
        password.fill("")
        page.screenshot(path=str(directory / "login-desktop.png"), full_page=True)
        password.fill(PASSWORD)
        page.get_by_role("button", name="进入光匣", exact=True).click()
        expect(page.get_by_label("作品链接或分享文案")).to_be_visible()
        page.reload()
        expect(page.get_by_label("作品链接或分享文案")).to_be_visible()
        submit(page, URL)
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        with page.expect_download() as download:
            page.get_by_role("link", name="下载视频", exact=True).click()
        target = tmp_path / "authenticated.mp4"
        download.value.save_as(target)
        assert target.read_bytes() == media_fixtures[0].read_bytes()
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(password).to_be_visible()
        assert page.request.get(login_server + "/api/jobs").status == 401
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("button", name="进入光匣", exact=True)).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(directory / "login-mobile.png"), full_page=True)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("browser", ["webkit"], indirect=True)
def test_webkit_login_and_task_writes_keep_same_origin(login_server, browser):
    # Catches Safari serializing Origin as null when same-origin fetch inherits no-referrer.
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    posts = []
    page.on("request", lambda request: posts.append((request.url.split(login_server)[-1], request.headers.get("origin"))) if request.method == "POST" else None)
    try:
        page.goto(login_server)
        page.get_by_label("访问密码", exact=True).fill(PASSWORD)
        with page.expect_response(lambda response: response.url.endswith("/auth/login")) as response:
            page.get_by_role("button", name="进入光匣", exact=True).click()
        assert response.value.status == 200, {"status": response.value.status, "posts": posts}
        expect(page.get_by_label("作品链接或分享文案")).to_be_visible()
        submit(page, URL)
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="退出登录", exact=True).click()
        expect(page.get_by_label("访问密码", exact=True)).to_be_visible()
        assert dict(posts) == {"/auth/login": login_server, "/api/jobs": login_server, "/auth/logout": login_server}
        assert page.request.get(login_server + "/api/jobs").status == 401
    finally:
        context.close()


def test_login_reports_proxy_rejection_as_request_error(login_server, browser):
    context = browser.new_context()
    page = context.new_page()
    try:
        page.route("**/auth/login", lambda route: route.fulfill(status=403, content_type="text/html", body="<h1>403 Forbidden</h1>"))
        page.goto(login_server)
        page.get_by_label("访问密码", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="进入光匣", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("请求校验")
        expect(page.get_by_role("button", name="进入光匣", exact=True)).to_be_enabled()
    finally:
        context.close()
