import json
import threading
import time
import zipfile
from dataclasses import replace

import pytest

from lumabox.web.config import InstanceLock, WebConfig, WebError
from lumabox.web.store import JobStore
from web_helpers import URL, WORK_ID, fixture_worker


@pytest.fixture
def config(tmp_path):
    return WebConfig(tmp_path / "data")


def test_queue_limit_fifo_and_running_is_not_pending(config):
    store = JobStore(config)
    first = store.create(URL)
    assert store.claim()["id"] == first["id"]
    queued = [store.create(URL) for _ in range(10)]
    with pytest.raises(WebError, match="QUEUE_FULL"):
        store.create(URL)
    assert store.claim() is None  # single running job enforced in storage too
    store.fail(first["id"], "INTERRUPTED")
    assert store.claim()["id"] == queued[0]["id"]


def test_input_canonicalization_discards_share_text_and_tracking(config):
    store = JobStore(config)
    job = store.create(f"分享文字 {URL}?secret=private 复制链接观看！")
    assert store.claim()["url"] == URL
    assert "private" not in json.dumps(job)
    with pytest.raises(WebError, match="INVALID_INPUT"):
        store.create("https://127.0.0.1/video/7686430330920075249")
    with pytest.raises(WebError, match="INVALID_INPUT"):
        store.create("x" * 4097)


def test_restart_fails_interrupted_job_but_keeps_pending(config):
    store = JobStore(config)
    active = store.create(URL)
    store.claim()
    waiting = store.create(URL)
    reopened = JobStore(config)
    reopened.recover()
    assert reopened.get(active["id"])["error"] == "INTERRUPTED"
    assert reopened.claim()["id"] == waiting["id"]


def test_same_data_directory_rejects_second_instance(config):
    with InstanceLock(config.data_dir):
        with pytest.raises(WebError, match="ALREADY_RUNNING"):
            with InstanceLock(config.data_dir):
                pass
    with InstanceLock(config.data_dir):
        pass


def test_real_child_success_only_publishes_verified_file(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create(URL)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0])
    assert worker.run_once()
    result = store.get(job["id"])
    assert (result["status"], result["actual_count"]) == ("success", 1)
    file = store.file(result["files"][0]["id"])
    assert store.file_path(file).read_bytes() == media_fixtures[0].read_bytes()
    assert "output_directory" not in json.dumps(result)
    assert str(config.data_dir) not in json.dumps(result)


@pytest.mark.parametrize("mode", ["hash", "missing", "escape", "identity", "decode", "count"])
def test_fake_success_never_publishes_files(config, tmp_path, media_fixtures, mode):
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0], mode).run_once()
    result = store.get(job["id"])
    assert result["status"] == "failed"
    assert result["files"] == []
    assert not (config.output_dir / job["id"]).exists()


def test_child_failure_keeps_only_fixed_error(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0], "failed").run_once()
    assert store.get(job["id"])["error"] == "AUTH_REQUIRED"
    assert "secret" not in json.dumps(store.get(job["id"]))


def test_child_timeout_ends_process_and_cleans_own_directory(config, tmp_path, media_fixtures):
    config = replace(config, task_timeout=.2)
    store = JobStore(config)
    job = store.create(URL)
    before = time.monotonic()
    fixture_worker(config, store, tmp_path, media_fixtures[0], "timeout").run_once()
    assert time.monotonic() - before < 12
    assert store.get(job["id"])["error"] == "TIMEOUT"
    assert not (config.output_dir / job["id"]).exists()


def test_shutdown_interrupts_child_before_cleaning_files(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create(URL)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0], "timeout")
    worker.start()
    deadline = time.monotonic() + 5
    while store.get(job["id"])["status"] != "running" and time.monotonic() < deadline:
        time.sleep(.02)
    worker.stop()
    assert store.get(job["id"])["error"] == "INTERRUPTED"
    assert not (config.output_dir / job["id"]).exists()


def test_gallery_zip_has_same_images_and_redacted_report(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create("https://v.douyin.com/test/")
    fixture_worker(config, store, tmp_path, media_fixtures[1], "gallery").run_once()
    result = store.get(job["id"])
    assert result["status"] == "success"
    assert [f["name"] for f in result["files"]] == ["01.jpg", f"{WORK_ID}-images.zip"]
    archive_path = store.file_path(store.file(result["files"][1]["id"]))
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["01.jpg", "report.json"]
        assert archive.read("01.jpg") == media_fixtures[1].read_bytes()
        assert str(config.data_dir) not in archive.read("report.json").decode()
    assert store.usage() >= 2 * media_fixtures[1].stat().st_size


def test_capacity_reserve_rejects_job_before_child_starts(config, tmp_path, media_fixtures):
    config = replace(config, capacity_bytes=100, reserve_bytes=100)
    store = JobStore(config)
    (config.output_dir / "used.bin").write_bytes(b"x")
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    assert store.get(job["id"])["error"] == "STORAGE_FULL"


def test_expiry_removes_only_completed_job_and_disables_file(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    file_id = store.get(job["id"])["files"][0]["id"]
    pending = store.create(URL)
    store.expire(now=time.time() + 86401)
    assert store.get(job["id"])["status"] == "expired"
    assert store.get(pending["id"])["status"] == "pending"
    assert not (config.output_dir / job["id"]).exists()
    with pytest.raises(WebError, match="NOT_FOUND"):
        store.file(file_id)


def test_symlink_file_is_never_served_or_followed_by_cleanup(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    file = store.file(store.get(job["id"])["files"][0]["id"])
    path = store.file_path(file)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"must survive")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Windows requires Developer Mode for symlinks")
    with pytest.raises(WebError, match="NOT_FOUND"):
        store.file_path(file)
    store.expire(now=time.time() + 86401)
    assert outside.read_bytes() == b"must survive"
