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
