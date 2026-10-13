import json
import time

from lumabox.web.app import create_app
import pytest
from fastapi.testclient import TestClient
from lumabox.web.config import WebConfig, WebError
from lumabox.web.store import JobStore
from web_helpers import URL, fixture_worker

PASSWORD = "test-local-password-123"
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def app(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "web")
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0])
    return create_app(config, PASSWORD, worker)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def login(client):
    response = client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}


def test_unauthenticated_jobs_and_files_are_private(client):
    for path in ("/api/session", "/api/jobs", "/api/jobs/" + "a" * 32, "/api/files/" + "a" * 32):
        assert client.get(path).status_code == 401


def test_password_has_no_default_and_short_password_is_rejected(tmp_path):
    with pytest.raises(WebError, match="INVALID_PASSWORD"):
        create_app(WebConfig(tmp_path), "short")


def test_session_cookie_logout_and_replay(client):
    headers = login(client)
    cookie = client.cookies.get("lumabox_session")
    assert client.get("/api/session").json()["csrf"] == headers["X-CSRF-Token"]
    response = client.post("/api/logout", json={}, headers=headers)
    assert response.status_code == 200
    client.cookies.set("lumabox_session", cookie)
    assert client.get("/api/jobs").status_code == 401


def test_cookie_flags_and_security_headers(client):
    response = client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://localhost:8765", "null"])
def test_login_requires_exact_same_origin(client, origin):
    headers = {} if origin is None else {"Origin": origin}
    assert client.post("/api/login", json={"password": PASSWORD}, headers=headers).status_code == 403


def test_host_rebinding_is_blocked_even_when_logged_in(client):
    login(client)
    assert client.get("/api/jobs", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/jobs", headers={"Host": "127.0.0.1:9000"}).status_code == 403


def test_csrf_required_for_submit_and_logout(client):
    headers = login(client)
    bad_headers = {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}
    assert client.post("/api/jobs", json={"input": URL, "authorized": True}, headers=bad_headers).status_code == 403
    assert client.post("/api/logout", json={}, headers=bad_headers).status_code == 403
    assert client.get("/api/session").status_code == 200
    assert client.post("/api/jobs", json={"input": URL, "authorized": True}, headers=headers).status_code == 202


def test_session_expires_after_eight_hours(client, monkeypatch):
    from lumabox.web import auth
    login(client)
    original_clock = auth.clock
    monkeypatch.setattr(auth, "clock", lambda: original_clock() + 28801)
    assert client.get("/api/session").status_code == 401


def test_five_wrong_passwords_rate_limit_next_attempt(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}, headers={"Origin": ORIGIN}).status_code == 401
    response = client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 429
    assert response.json()["error"] == "LOGIN_LIMIT"


def test_json_only_and_request_body_limit(client):
    assert client.post("/api/login", content="password=whatever", headers={"Origin": ORIGIN}).status_code == 415
    response = client.post("/api/login", content=b"x" * 8193, headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 413
    # Streaming input has no trusted Content-Length, and still must be bounded.
    response = client.post("/api/login", content=iter([b"x" * 5000, b"x" * 4000]), headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 413


@pytest.mark.parametrize("body", [
    {"input": URL, "authorized": False}, {"input": URL, "authorized": 1},
    {"input": "https://evil.example/file", "authorized": True},
    {"input": URL, "authorized": True, "output": "../escape"},
    {"input": [URL], "authorized": True},
])
def test_invalid_or_unauthorized_submission_is_rejected(client, body):
    headers = login(client)
    assert client.post("/api/jobs", json=body, headers=headers).status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_real_worker_download_route_returns_exact_bytes_and_no_paths(client, app, media_fixtures):
    headers = login(client)
    response = client.post("/api/jobs", json={"input": "分享文字 " + URL, "authorized": True}, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] in ("success", "failed"):
            break
        time.sleep(.03)
    assert result["status"] == "success"
    assert str(app.state.store.config.data_dir) not in json.dumps(result)
    file_id = result["files"][0]["id"]
    download = client.get(f"/api/files/{file_id}")
    assert download.content == media_fixtures[0].read_bytes()
    assert download.headers["content-type"].startswith("video/mp4")
    assert "attachment" in download.headers["content-disposition"]
    assert client.get("/api/files/../../jobs.sqlite3").status_code == 404
    assert client.get("/outputs/" + job_id).status_code == 404
    client.post("/api/logout", json={}, headers=headers)
    assert client.get(f"/api/files/{file_id}").status_code == 401


def test_malformed_json_never_echoes_input_or_traceback(client):
    response = client.post("/api/login", content='{ "secret": invalid }', headers={"Origin": ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"
    assert "secret" not in response.text
