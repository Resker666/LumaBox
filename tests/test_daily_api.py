import hashlib
import asyncio
import time
from pathlib import Path
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.requests import ClientDisconnect

from lumabox import __version__
from lumabox.web.app import create_app
from lumabox.web.config import WebConfig, WebError
from lumabox.web.store import JobStore
from lumabox.web.media import preview_response
from web_helpers import URL, WORK_ID

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def daily(tmp_path):
    config = WebConfig(tmp_path / "data")
    app = create_app(config)
    # Route/storage tests intentionally don't start a downloader thread.
    client = TestClient(app, base_url=ORIGIN)
    try:
        yield client, app.state.store
    finally:
        client.close()


@contextmanager
def routed_client(config):
    client = TestClient(create_app(config), base_url=ORIGIN)
    try:
        yield client
    finally:
        client.close()


def completed(store, fixture, title="测试作品"):
    job = store.create(URL, force_download=True)
    claimed = store.claim()
    assert claimed and claimed["id"] == job["id"]
    directory = store.config.output_dir / job["id"] / (WORK_ID + "-" + "a" * 32)
    directory.mkdir(parents=True)
    data = fixture.read_bytes()
    path = directory / (WORK_ID + ".mp4")
    path.write_bytes(data)
    store.finish(job["id"], {"work_id": WORK_ID, "kind": "video", "title": title,
                 "expected_count": 1, "actual_count": 1, "bytes": len(data)},
                 [{"relative_path": str(path.relative_to(store.config.output_dir)), "name": path.name,
                   "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "mime": "video/mp4", "width": 64, "height": 48, "duration": .4}])
    return store.get(job["id"]), path, data


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.get("/api/session").json()["csrf"]}


def test_launcher_identity_distinguishes_instance_and_version(daily):
    client, store = daily
    settings = client.get("/api/session").json()
    assert settings["application"] == "lumabox"
    assert settings["version"] == __version__
    assert Path(settings["data_directory"]) == store.config.data_dir


def test_title_is_stored_and_literal_search_is_paginated(tmp_path, media_fixtures):
    store = JobStore(WebConfig(tmp_path / "data"))
    first, _, _ = completed(store, media_fixtures[0], "100%_雪山")
    second, _, _ = completed(store, media_fixtures[0], "100公里雪山")
    assert first["title"] == "100%_雪山"
    with routed_client(store.config) as client:
        found = client.get("/api/jobs", params={"q": "%_", "page_size": 1}).json()
        assert found["total"] == 1 and found["jobs"][0]["id"] == first["id"]
        page = client.get("/api/jobs", params={"status": "success", "page_size": 1, "page": 1}).json()
        assert page["total"] == 2 and page["jobs"][0]["id"] == second["id"]
        next_page = client.get("/api/jobs", params={"status": "success", "page_size": 1, "page": 2}).json()
        assert next_page["jobs"][0]["id"] == first["id"]


@pytest.mark.parametrize("query", [{"page": 0}, {"page_size": 101}, {"status": "wat"}, {"q": "a" * 201}, {"page": "NaN"}])
def test_history_queries_are_bounded(daily, query):
    client, _ = daily
    response = client.get("/api/jobs", params=query)
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"


def test_progress_is_bound_to_work_and_cannot_regress_or_revive_job(tmp_path):
    store = JobStore(WebConfig(tmp_path / "data"))
    job = store.create(URL)
    store.claim()
    event = {"stage": "media_transfer", "work_id": WORK_ID, "kind": "video", "title": "  雪山\x00  日出 ",
             "bytes_received": 25, "bytes_total": 100, "completed_count": 0, "expected_count": 1, "resource_index": 1}
    assert store.update_progress(job["id"], event)
    current = store.get(job["id"])
    assert current["title"] == "雪山 日出"
    assert current["progress"]["bytes_received"] == 25
    assert current["progress"]["bytes_total"] == 100
    assert current["progress"]["elapsed_seconds"] >= 0
    assert not store.update_progress(job["id"], {**event, "work_id": "1111111111111111111"})
    assert not store.update_progress(job["id"], {**event, "bytes_received": 24})
    assert not store.update_progress(job["id"], {**event, "bytes_received": True})
    assert store.get(job["id"])["progress"]["bytes_received"] == 25
    store.fail(job["id"], "DOWNLOAD_FAILED")
    frozen = store.get(job["id"])
    assert not store.update_progress(job["id"], {**event, "bytes_received": 80})
    assert store.get(job["id"])["status"] == "failed"
    assert store.get(job["id"])["progress"] == frozen["progress"]


def test_preview_ranges_head_and_zip_rejection(tmp_path, media_fixtures):
    store = JobStore(WebConfig(tmp_path / "data"))
    job, path, data = completed(store, media_fixtures[0])
    file_id = job["files"][0]["id"]
    with routed_client(store.config) as client:
        preview = f"/api/files/{file_id}/preview"
        response = client.get(preview, headers={"Range": "bytes=4-11"})
        assert response.status_code == 206 and response.content == data[4:12]
        assert response.headers["content-range"] == f"bytes 4-11/{len(data)}"
        assert response.headers["content-length"] == "8"
        assert response.headers["content-disposition"].startswith("inline;")
        assert client.get(preview, headers={"Range": "bytes=-7"}).content == data[-7:]
        assert client.get(preview, headers={"Range": "bytes=12-"}).content == data[12:]
        response = client.head(preview, headers={"Range": "bytes=4-11"})
        assert response.status_code == 206 and response.content == b""
        assert response.headers["content-length"] == "8"
        response = client.get(preview, headers={"Range": f"bytes={len(data)}-"})
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(data)}"
        assert not store.downloads
        with store.connection() as db:
            db.execute("UPDATE files SET mime='application/zip' WHERE id=?", (file_id,))
        assert client.get(preview).status_code == 415
        assert path.read_bytes() == data


@pytest.mark.parametrize("range_header", ["items=0-1", "bytes=9-1", "bytes=-0", "bytes=0-1,4-5", "bytes=a-1"])
def test_preview_bad_ranges_release_file_lease(tmp_path, media_fixtures, range_header):
    store = JobStore(WebConfig(tmp_path / "data"))
    job, _, _ = completed(store, media_fixtures[0])
    with routed_client(store.config) as client:
        response = client.get(f"/api/files/{job['files'][0]['id']}/preview", headers={"Range": range_header})
        assert response.status_code == 416 and not store.downloads


def test_remove_requires_csrf_and_preserves_bytes_and_existing_lease(tmp_path, media_fixtures):
    store = JobStore(WebConfig(tmp_path / "data"))
    job, path, data = completed(store, media_fixtures[0])
    with routed_client(store.config) as client:
        file_id = job["files"][0]["id"]
        route = f"/api/jobs/{job['id']}/remove"
        assert client.post(route, json={}, headers={"Origin": ORIGIN}).status_code == 403
        assert client.post(route, json={"path": "C:/Windows"}, headers=headers(client)).status_code == 400
        with store.open_file(file_id) as (_, stream):
            assert client.post(route, json={}, headers=headers(client)).status_code == 200
            assert stream.read() == data
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404
        assert client.get(f"/api/files/{file_id}/preview").status_code == 404
        assert client.get("/api/jobs").json()["total"] == 0
        assert path.read_bytes() == data


def test_clear_failed_and_active_removal_keep_success_and_pending(tmp_path, media_fixtures):
    store = JobStore(WebConfig(tmp_path / "data"))
    success, path, data = completed(store, media_fixtures[0])
    for _ in range(3):
        job = store.create(URL, force_download=True)
        store.fail(job["id"], "DOWNLOAD_FAILED")
    pending = store.create(URL, force_download=True)
    with routed_client(store.config) as client:
        with store.connection() as db:
            db.execute("UPDATE jobs SET status='pending' WHERE id=?", (pending["id"],))
        assert client.post(f"/api/jobs/{pending['id']}/remove", json={}, headers=headers(client)).status_code == 409
        assert client.post("/api/jobs/clear-failed", json={}, headers=headers(client)).json() == {"removed": 3}
        assert client.get(f"/api/jobs/{success['id']}").json()["status"] == "success"
        assert path.read_bytes() == data


def test_preview_transport_failure_releases_real_open_file(tmp_path, media_fixtures):
    store = JobStore(WebConfig(tmp_path / "data"))
    job, path, data = completed(store, media_fixtures[0])
    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}
    response = preview_response(store, job["files"][0]["id"], Request(scope))
    assert store.downloads == {job["id"]: 1}
    async def send(message):
        raise OSError("Controlled closed transport")
    async def receive():
        return {"type": "http.disconnect"}
    with pytest.raises(ClientDisconnect):
        asyncio.run(response(scope, receive, send))
    assert not store.downloads and path.read_bytes() == data


def test_failure_title_cannot_change_bound_kind(tmp_path):
    store = JobStore(WebConfig(tmp_path / "data"))
    job = store.create(URL)
    store.fail(job["id"], "VERIFY_FAILED", {"work_id": WORK_ID, "kind": "gallery", "title": "错误作品标题"})
    result = store.get(job["id"])
    assert result["kind"] == "video" and result["title"] is None
