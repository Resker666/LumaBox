import os
import pytest
from lumabox.web.store import JobStore
from lumabox.web.config import WebError
from test_daily_api import completed, routed_client, headers
from web_helpers import URL, fixture_worker


def test_unknown_file_counted_without_becoming_collection(web_config):
    store = JobStore(web_config)
    (web_config.output_dir / "user-file.bin").write_bytes(b"abc")
    summary = store.storage.summary(refresh=True)
    assert summary["output_bytes"] == summary["other_bytes"] == 3
    assert summary["saved_bytes"] == summary["saved_count"] == 0
    assert summary["disk_free_bytes"] > 0 and summary["complete"]
    assert store.storage.items()["total"] == 0


def test_hidden_collection_stays_manageable_and_restore_invalidates(web_config, media_fixtures):
    store = JobStore(web_config)
    job, path, data = completed(store, media_fixtures[0])
    initial = store.storage.summary()
    assert initial["saved_bytes"] == len(data) and initial["hidden_bytes"] == 0
    store.remove(job["id"])
    assert store.storage.summary()["hidden_bytes"] == len(data)
    item = store.storage.items(scope="hidden")["items"][0]
    assert item["id"] == job["id"] and item["bytes"] == len(data) and item["hidden"]
    assert path.read_bytes() == data
    store.restore(job["id"])
    assert store.storage.summary()["hidden_bytes"] == 0
    assert store.storage.items(scope="hidden")["total"] == 0


def test_gallery_archive_and_report_have_distinct_accounting(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config); job = store.create("https://v.douyin.com/test/")
    fixture_worker(web_config, store, tmp_path, media_fixtures[1], "gallery").run_once()
    published = store.get(job["id"])
    assert published["status"] == "success"
    media_bytes = sum(f["bytes"] for f in published["files"])
    summary = store.storage.summary()
    assert summary["saved_bytes"] == media_bytes
    assert summary["output_bytes"] > media_bytes
    assert summary["other_bytes"] == summary["output_bytes"] - media_bytes
    item = store.storage.items()["items"][0]
    assert item["bytes"] == summary["output_bytes"] and item["file_count"] == 3


def test_cache_refresh_expiry_and_publication_invalidation(web_config, monkeypatch, media_fixtures):
    import lumabox.web.storage as storage
    now = [100.0]
    monkeypatch.setattr(storage.time, "monotonic", lambda: now[0])
    store = JobStore(web_config)
    (web_config.output_dir / "other").write_bytes(b"abc")
    assert store.storage.summary()["output_bytes"] == 3
    (web_config.output_dir / "other").write_bytes(b"abcdef")
    now[0] = 129
    assert store.storage.summary()["output_bytes"] == 3
    now[0] = 130
    assert store.storage.summary()["output_bytes"] == 6
    (web_config.output_dir / "other").write_bytes(b"a")
    assert store.storage.summary(refresh=True)["output_bytes"] == 1
    completed(store, media_fixtures[0])
    assert store.storage.summary()["saved_count"] == 1


def test_scan_permission_error_is_reported_as_incomplete(web_config, monkeypatch):
    store = JobStore(web_config)
    hidden = web_config.output_dir / "blocked"; hidden.mkdir()
    (hidden / "file").write_bytes(b"secret")
    original = os.scandir
    def scan(path):
        if os.fspath(path) == str(hidden):
            raise PermissionError("controlled")
        return original(path)
    monkeypatch.setattr(os, "scandir", scan)
    assert store.storage.summary(refresh=True)["complete"] is False


def test_missing_files_are_not_counted_as_current_storage(web_config, media_fixtures):
    store = JobStore(web_config); job, path, _ = completed(store, media_fixtures[0])
    path.unlink()
    summary = store.storage.summary(refresh=True)
    assert summary["output_bytes"] == summary["saved_bytes"] == 0
    assert store.storage.items()["total"] == 0


def test_storage_api_validation_and_read_only_behavior(web_config):
    with routed_client(web_config) as client:
        assert client.get("/api/storage").status_code == 200
        assert client.get("/api/storage?refresh=2").status_code == 400
        assert client.get("/api/storage/items?page=0").status_code == 400
        assert client.get("/api/storage/items?scope=unknown").status_code == 400
        assert client.get("/api/storage/items?page_size=1000").status_code == 400
        assert client.get("/api/storage/items").json()["total"] == 0


def test_clearing_failed_records_invalidates_retained_file_list(web_config):
    store = JobStore(web_config); job = store.create(URL)
    store.fail(job["id"], "DOWNLOAD_FAILED")
    directory = web_config.output_dir / job["id"]; directory.mkdir()
    (directory / "cli-result.jsonl").write_text("private")
    assert store.storage.items(scope="hidden")["total"] == 0
    store.clear_failed()
    assert store.storage.items(scope="hidden")["total"] == 1


import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import pytest
from starlette.requests import Request, ClientDisconnect
from lumabox.web.app import create_app
from lumabox.web.store import JobStore
from lumabox.web.config import WebError
from test_daily_api import completed, routed_client, headers
from web_helpers import URL


def test_pending_job_cannot_be_purged(web_config):
    store = JobStore(web_config); job = store.create(URL)
    with pytest.raises(WebError, match="JOB_ACTIVE"):
        store.storage.purge(job["id"])
    assert store.get(job["id"])["status"] == "pending"


def test_purge_hidden_work_is_idempotent_and_preserves_other_work(web_config, media_fixtures):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    other, other_path, other_data = completed(store, media_fixtures[0])
    file_id = job["files"][0]["id"]; store.remove(job["id"])
    assert store.storage.purge(job["id"])["status"] == "deleted"
    assert not path.exists() and other_path.read_bytes() == other_data
    assert store.storage.purge(job["id"])["deleted_bytes"] == 0
    assert store.storage.summary()["saved_count"] == 1
    with pytest.raises(WebError, match="NOT_FOUND"):
        store.file(file_id)
    with pytest.raises(WebError, match="RESULT_UNAVAILABLE"):
        store.restore(job["id"])


def test_purge_refuses_open_file_lease(web_config, media_fixtures):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    with store.open_file(job["files"][0]["id"]) as (_, stream):
        with pytest.raises(WebError, match="FILE_IN_USE"):
            store.storage.purge(job["id"])
        assert stream.read() == data and path.read_bytes() == data
    assert store.storage.purge(job["id"])["status"] == "deleted"


def test_unknown_extra_file_prevents_entire_purge(web_config, media_fixtures):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    extra = path.parent / "my-notes.txt"; extra.write_text("keep")
    with pytest.raises(WebError, match="UNSAFE_DELETE"):
        store.storage.purge(job["id"])
    assert path.read_bytes() == data and extra.read_text() == "keep"


def test_database_path_escape_is_never_deleted(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    outside = tmp_path / "outside.mp4"; outside.write_bytes(b"keep")
    with store.connection() as db:
        db.execute("UPDATE files SET relative_path='../outside.mp4' WHERE job_id=?", (job["id"],))
    with pytest.raises(WebError, match="UNSAFE_DELETE"):
        store.storage.purge(job["id"])
    assert outside.read_bytes() == b"keep" and path.read_bytes() == data


def test_failed_delete_blocks_reads_and_recovers_only_confirmed_intent(web_config, media_fixtures, monkeypatch):
    store = JobStore(web_config); job, path, _ = completed(store, media_fixtures[0])
    untouched, other_path, other_data = completed(store, media_fixtures[0])
    original = Path.unlink
    def denied(target, *args, **kwargs):
        if target == path: raise PermissionError("controlled")
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", denied)
    assert store.storage.purge(job["id"])["status"] == "delete_failed"
    with pytest.raises(WebError): store.file(job["files"][0]["id"])
    assert store.storage.items()["items"][1]["storage_state"] == "delete_failed"
    monkeypatch.setattr(Path, "unlink", original)
    reopened = JobStore(web_config); reopened.storage.recover_deletions()
    assert not path.exists() and other_path.read_bytes() == other_data


def test_deletion_intent_never_applies_to_changed_output(web_config, tmp_path, media_fixtures, monkeypatch):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    original = Path.unlink
    def interrupted(target, *args, **kwargs):
        if target == path: raise KeyboardInterrupt()
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(KeyboardInterrupt): store.storage.purge(job["id"])
    monkeypatch.setattr(Path, "unlink", original)
    moved = JobStore(replace(web_config, output=tmp_path / "new-output"))
    moved.storage.recover_deletions()
    with pytest.raises(WebError, match="OUTPUT_CHANGED"): moved.storage.purge(job["id"])
    assert path.read_bytes() == data
    store.storage.recover_deletions()
    assert not path.exists()


def test_purge_requires_explicit_confirmation_and_csrf(web_config, media_fixtures):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    route = f"/api/jobs/{job['id']}/purge"
    with routed_client(web_config) as client:
        assert client.get(route).status_code == 405
        assert client.post(route, json={"confirm": True}).status_code == 403
        for body in ({}, {"confirm": False}, {"confirm": 1}, {"confirm": True, "path": "C:/Windows"}):
            assert client.post(route, json=body, headers=headers(client)).status_code == 400
            assert path.read_bytes() == data
        assert client.post(route, json={"confirm": True}, headers=headers(client)).json()["status"] == "deleted"


def test_download_send_failure_releases_lease_before_purge(web_config, media_fixtures):
    app = create_app(web_config); store = app.state.store
    job, _, _ = completed(store, media_fixtures[0])
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/files/{file_id}")
    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}
    response = endpoint(job["files"][0]["id"], Request(scope))
    assert store.downloads == {job["id"]: 1}
    async def send(message): raise OSError("controlled transport")
    async def receive(): return {"type": "http.disconnect"}
    with pytest.raises(ClientDisconnect): asyncio.run(response(scope, receive, send))
    assert not store.downloads
    assert store.storage.purge(job["id"])["status"] == "deleted"


def test_concurrent_purge_only_removes_the_owned_files_once(web_config, media_fixtures):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.storage.purge(job["id"]), range(2)))
    assert all(r["status"] == "deleted" for r in results)
    assert sum(r["deleted_bytes"] for r in results) == len(data)
    assert not path.exists()


@pytest.mark.parametrize("manifest", ['[""]', '{"unexpected": true}', 'not-json', 'null'])
def test_corrupt_deletion_manifest_is_refused_without_deleting_files(web_config, media_fixtures, manifest):
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    with store.connection() as db:
        db.execute("UPDATE jobs SET storage_state='deleting',purge_root=?,purge_manifest=? WHERE id=?",
                   (str(web_config.output_dir), manifest, job["id"]))
    store.storage.recover_deletions()
    assert path.read_bytes() == data
    with store.connection() as db:
        assert db.execute("SELECT storage_state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "delete_failed"


def test_partial_delete_recovers_remaining_owned_files(web_config, media_fixtures, monkeypatch):
    store = JobStore(web_config); job, path, _ = completed(store, media_fixtures[0])
    report = path.parent / "report.json"; report.write_text("{}")
    original, removed = Path.unlink, []
    def once(target, *args, **kwargs):
        if target.is_relative_to(web_config.output_dir / job["id"]):
            if removed: raise PermissionError("controlled second file")
            original(target, *args, **kwargs); removed.append(target)
        else: return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", once)
    assert store.storage.purge(job["id"])["status"] == "delete_failed"
    assert len(removed) == 1 and sum(p.exists() for p in (path, report)) == 1
    monkeypatch.setattr(Path, "unlink", original)
    JobStore(web_config).storage.recover_deletions()
    assert not path.exists() and not report.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_purge_and_inventory_never_follow_windows_junction(web_config, tmp_path, media_fixtures):
    import _winapi
    store = JobStore(web_config); job, path, data = completed(store, media_fixtures[0])
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "keep").write_bytes(b"keep")
    junction = path.parent / "linked"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        assert store.storage.summary(refresh=True)["complete"] is False
        with pytest.raises(WebError, match="UNSAFE_DELETE"): store.storage.purge(job["id"])
        assert path.read_bytes() == data and (outside / "keep").read_bytes() == b"keep"
    finally:
        junction.rmdir()
