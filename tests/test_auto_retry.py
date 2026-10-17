import time
import pytest
from lumabox.web.store import JobStore
from lumabox.web.config import WebError
from web_helpers import URL, retry_worker, fixture_worker

FAILURE = {"diagnostics": {"network_reason": "CONNECTION_RESET", "stage": "media_transfer", "url": "secret"}}


def test_retry_budget_deadline_progress_and_restart(web_config):
    store = JobStore(web_config)
    job = store.create(URL)
    claimed = store.claim()
    assert claimed["attempt_count"] == 1
    assert claimed["deadline_at"] == claimed["started_at"] + 3600
    now = claimed["started_at"] + 1
    assert store.update_progress(job["id"], {"stage": "media_transfer", "work_id": job["work_id"],
        "kind": "video", "bytes_received": 50, "expected_count": 1})
    assert store.schedule_retry(job["id"], "DOWNLOAD_FAILED", FAILURE, now=now)
    waiting = store.get(job["id"])
    assert waiting["status"] == "retry_wait" and waiting["retry_at"] == now + 10
    assert "secret" not in str(waiting)
    with pytest.raises(WebError, match="JOB_ACTIVE"):
        store.remove(job["id"])
    reopened = JobStore(web_config)
    reopened.recover()
    assert reopened.claim()["id"] == job["id"]
    assert reopened.begin_retry(job["id"], now=now + 9) is None
    second = reopened.begin_retry(job["id"], now=now + 10)
    assert second["attempt_count"] == 2 and second["deadline_at"] == claimed["deadline_at"]
    assert reopened.get(job["id"])["progress"].get("bytes_received", 0) == 0
    assert not reopened.schedule_retry(job["id"], "DOWNLOAD_FAILED", FAILURE, now=now + 11)
    reopened.fail(job["id"], "DOWNLOAD_FAILED", FAILURE)
    assert [a["error"] for a in reopened.get(job["id"])["attempts"]] == ["DOWNLOAD_FAILED"] * 2


def test_cancel_wait_and_total_deadline_prevent_retry(web_config):
    store = JobStore(web_config)
    job = store.create(URL); claimed = store.claim()
    assert not store.schedule_retry(job["id"], "DOWNLOAD_FAILED", FAILURE, now=claimed["deadline_at"] - 5)
    assert store.schedule_retry(job["id"], "DOWNLOAD_FAILED", FAILURE)
    assert store.cancel(job["id"])["status"] == "cancelled"
    assert store.begin_retry(job["id"], now=time.time() + 100) is None
    assert store.search(status="retry_wait")["total"] == 0


def test_waiting_retry_owns_processing_position(web_config):
    store = JobStore(web_config)
    job = store.create(URL); store.claim()
    assert store.schedule_retry(job["id"], "RESOURCE_TIMEOUT", {})
    other = store.create("https://www.douyin.com/video/7686430330920075250")
    assert store.claim()["id"] == job["id"]
    assert store.get(other["id"])["status"] == "pending"


def test_real_child_retries_in_same_job(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    job = store.create(URL)
    worker = retry_worker(web_config, store, tmp_path, media_fixtures[0])
    assert worker.run_once()
    result = store.get(job["id"])
    assert result["status"] == "success"
    assert result["attempt_count"] == 2 and len(result["attempts"]) == 2
    assert (tmp_path / "attempt-count.txt").read_text() == "2"
    assert store.file_path(store.file(result["files"][0]["id"])).read_bytes() == media_fixtures[0].read_bytes()


def test_two_failures_stop_after_second_child(web_config, tmp_path, media_fixtures, monkeypatch):
    monkeypatch.setattr("lumabox.web.store.RETRY_DELAY_SECONDS", .05)
    store = JobStore(web_config); job = store.create(URL)
    worker = retry_worker(web_config, store, tmp_path, media_fixtures[0], failures=5)
    worker.run_once()
    assert (tmp_path / "attempt-count.txt").read_text() == "2"
    result = store.get(job["id"])
    assert result["status"] == "failed" and len(result["attempts"]) == 2
    assert not (web_config.output_dir / job["id"]).exists()


def wait_state(store, job_id, status):
    deadline = time.monotonic() + 5
    while store.get(job_id)["status"] != status and time.monotonic() < deadline:
        time.sleep(.01)
    assert store.get(job_id)["status"] == status


def test_cancel_wait_never_starts_second_child(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config); job = store.create(URL)
    worker = retry_worker(web_config, store, tmp_path, media_fixtures[0])
    worker.start()
    try:
        wait_state(store, job["id"], "retry_wait")
        store.cancel(job["id"])
        wait_state(store, job["id"], "cancelled")
    finally:
        worker.stop()
    assert (tmp_path / "attempt-count.txt").read_text() == "1"


def test_shutdown_preserves_retry_wait_and_budget(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config); job = store.create(URL)
    worker = retry_worker(web_config, store, tmp_path, media_fixtures[0])
    worker.start()
    try:
        wait_state(store, job["id"], "retry_wait")
    finally:
        worker.stop()
    saved = store.get(job["id"])
    assert saved["status"] == "retry_wait" and saved["attempt_count"] == 1
    with store.connection() as db:
        db.execute("UPDATE jobs SET retry_at=? WHERE id=?", (time.time() - 1, job["id"]))
    resumed = retry_worker(web_config, JobStore(web_config), tmp_path, media_fixtures[0])
    assert resumed.run_once()
    assert store.get(job["id"])["status"] == "success"
    assert store.get(job["id"])["attempt_count"] == 2


def test_cleanup_failure_does_not_retry(web_config, tmp_path, media_fixtures, monkeypatch):
    store = JobStore(web_config); job = store.create(URL)
    monkeypatch.setattr(store, "try_cleanup", lambda job_id: False)
    retry_worker(web_config, store, tmp_path, media_fixtures[0]).run_once()
    assert store.get(job["id"])["status"] == "failed"
    assert store.get(job["id"])["error"] == "CLEANUP_FAILED"
    assert (tmp_path / "attempt-count.txt").read_text() == "1"


def test_nonobject_cli_report_fails_without_stranding_queue(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config); first = store.create(URL)
    worker = fixture_worker(web_config, store, tmp_path, media_fixtures[0])
    worker.script.write_text("print('[]')", encoding="utf-8")
    assert worker.run_once()
    assert store.get(first["id"])["error"] == "VERIFY_FAILED"
    second = store.create(URL)
    fixture_worker(web_config, store, tmp_path, media_fixtures[0]).run_once()
    assert store.get(second["id"])["status"] == "success"


@pytest.mark.parametrize("action", ["remove", "purge"])
def test_cancel_then_hide_retry_waiter_keeps_queue_alive(web_config, tmp_path, media_fixtures, monkeypatch, action):
    import threading
    store = JobStore(web_config); first = store.create(URL); store.claim()
    assert store.schedule_retry(first["id"], "RESOURCE_TIMEOUT", {})
    following = store.create("https://v.douyin.com/next-work/")
    worker = fixture_worker(web_config, store, tmp_path, media_fixtures[0])
    waiting, resume = threading.Event(), threading.Event()
    original = worker._wait_retry
    def pause(job):
        waiting.set()
        assert resume.wait(5)
        return original(job)
    monkeypatch.setattr(worker, "_wait_retry", pause)
    worker.start()
    try:
        assert waiting.wait(5)
        store.cancel(first["id"])
        if action == "remove": store.remove(first["id"])
        else: assert store.storage.purge(first["id"])["status"] == "deleted"
        resume.set()
        deadline = time.monotonic() + 5
        while worker.thread.is_alive() and store.get(following["id"])["status"] == "pending" and time.monotonic() < deadline:
            time.sleep(.01)
        assert worker.thread.is_alive(), "hiding a cancelled waiter killed the worker"
        wait_state(store, following["id"], "success")
        with store.connection() as db:
            assert db.execute("SELECT attempt_count FROM jobs WHERE id=?", (first["id"],)).fetchone()[0] == 1
    finally:
        resume.set()
        worker.stop()
