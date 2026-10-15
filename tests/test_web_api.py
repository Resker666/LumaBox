import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lumabox.web.app import create_app
from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from web_helpers import URL, WORK_ID, fixture_worker

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def app(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "web")
    store = JobStore(config)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0])
    return create_app(config, worker=worker)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def local_headers(client):
    response = client.get("/api/session")
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}


def complete(client, headers, text=URL):
    response = client.post("/api/jobs", json={"input": text}, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["id"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("success", "failed"):
            return job
        time.sleep(.03)
    pytest.fail("Controlled local task did not finish")


def test_local_page_and_history_need_no_password(client, app):
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json()["video_limit_mib"] == 1024
    assert Path(response.json()["output_directory"]) == app.state.store.config.output_dir
    assert client.get("/api/jobs").json() == {"jobs": [], "total": 0, "page": 1, "page_size": 20}


def test_security_headers_still_apply_without_login(client):
    response = client.get("/api/session")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://localhost:8765", "null"])
def test_writes_require_exact_same_origin(client, origin):
    headers = local_headers(client)
    if origin is None:
        del headers["Origin"]
    else:
        headers["Origin"] = origin
    assert client.post("/api/jobs", json={"input": URL}, headers=headers).status_code == 403


def test_host_rebinding_and_cross_site_reads_are_blocked(client):
    assert client.get("/api/jobs", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/jobs", headers={"Host": "127.0.0.1:9000"}).status_code == 403
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_writes_need_bootstrap_csrf_but_no_user_confirmation(client):
    headers = local_headers(client)
    assert client.post("/api/jobs", json={"input": URL}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/jobs", json={"input": URL}, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/jobs", json={"input": URL}, headers=headers).status_code == 202


def test_json_and_streaming_body_are_bounded(client):
    headers = local_headers(client)
    assert client.post("/api/jobs", content="input=whatever", headers=headers).status_code == 415
    json_headers = {**headers, "Content-Type": "application/json"}
    assert client.post("/api/jobs", content=b"x" * 8193, headers=json_headers).status_code == 413
    assert client.post("/api/jobs", content=iter([b"x" * 5000, b"x" * 4000]), headers=json_headers).status_code == 413


@pytest.mark.parametrize("body", [
    {"input": "https://evil.example/file"}, {"input": URL, "output": "../escape"},
    {"input": [URL]}, {"input": URL, "video_limit_mib": 0}, {"input": URL, "video_limit_mib": True},
    {"input": URL, "video_limit_mib": 8193},
])
def test_malformed_input_and_caller_paths_are_rejected(client, body):
    assert client.post("/api/jobs", json=body, headers=local_headers(client)).status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_adjustable_limit_is_persisted_per_job(client):
    response = client.post("/api/jobs", json={"input": URL, "video_limit_mib": 2048}, headers=local_headers(client))
    assert response.status_code == 202
    assert response.json()["video_limit_mib"] == 2048


def test_real_download_and_local_folder_path(client, app, media_fixtures):
    job = complete(client, local_headers(client), "分享文字 " + URL)
    assert job["status"] == "success"
    expected = app.state.store.config.output_dir / job["id"] / (WORK_ID + "-" + "a" * 32)
    assert Path(job["output_directory"]) == expected
    download = client.get(f"/api/files/{job['files'][0]['id']}")
    assert download.content == media_fixtures[0].read_bytes()
    assert "attachment" in download.headers["content-disposition"]
    assert client.get("/api/files/../../jobs.sqlite3").status_code == 404
    assert client.get("/outputs/" + job["id"]).status_code == 404


def test_open_folder_uses_verified_job_directory_and_rejects_caller_path(client, app, monkeypatch):
    import lumabox.web.app as web_app
    opened = []
    monkeypatch.setattr(web_app, "open_folder", lambda path: opened.append(path), raising=False)
    headers = local_headers(client)
    job = complete(client, headers)
    route = f"/api/jobs/{job['id']}/open-folder"
    assert client.post(route, json={"path": "C:/Windows"}, headers=headers).status_code == 400
    assert opened == []
    assert client.post(route, json={}, headers=headers).status_code == 200
    assert opened == [Path(job["output_directory"])]
    assert client.post(route, json={}, headers={"Origin": ORIGIN}).status_code == 403


def test_retry_reuses_saved_link_with_current_limit_and_no_duplicate(client, app):
    headers = local_headers(client)
    job = app.state.store.create(URL)
    app.state.store.fail(job["id"], "SIZE_LIMIT")
    response = client.post(f"/api/jobs/{job['id']}/retry", json={"video_limit_mib": 2048}, headers=headers)
    assert response.status_code == 202
    retry = response.json()
    assert retry["id"] != job["id"] and retry["source_url"] == URL
    assert retry["video_limit_mib"] == 2048
    with app.state.store.connection() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (retry["id"],))
    assert client.post(f"/api/jobs/{job['id']}/retry", json={}, headers=headers).status_code == 409


def test_malformed_json_never_echoes_input(client):
    headers = {**local_headers(client), "Content-Type": "application/json"}
    response = client.post("/api/jobs", content='{ "secret": invalid }', headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"
    assert "secret" not in response.text
