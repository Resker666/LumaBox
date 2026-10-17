import time
import pytest
from lumabox.web.store import JobStore
from lumabox.web.config import WebError
from web_helpers import URL

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
