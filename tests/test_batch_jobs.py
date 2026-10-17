"""Batch operations reuse the existing queue and keep per-item outcomes."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from fastapi.testclient import TestClient

from lumabox.web.app import create_app
from lumabox.web.store import JobStore
from lumabox.web.worker import JobWorker
from test_daily_api import ORIGIN, completed, headers
from web_helpers import URL, fixture_worker


def url(index):
    return f"https://www.douyin.com/video/{7686430330920075249 + index}"


@pytest.fixture
def batch_client(web_config):
    store = JobStore(web_config)
    worker = JobWorker(web_config, store)
    client = TestClient(create_app(web_config, worker), base_url=ORIGIN)
    try:
        yield client, store, worker
    finally:
        client.close()


def post(client, action="", **body):
    return client.post("/api/jobs/batch" + action, json=body, headers=headers(client))


def test_batch_submission_reports_each_link_and_keeps_normalized_order(batch_client):
    client, store, _ = batch_client
    text = f"第一份 {URL}?tracking=private，复制\n{url(1)}\n{URL}/\nhttp://www.douyin.com/video/7686430330920075251"
    response = post(client, input=text, video_limit_mib=512, include_audio=True)
    assert response.status_code == 202
    result = response.json()
    assert [entry["status"] for entry in result["results"]] == ["queued", "queued", "already_queued", "invalid"]
    assert [entry["index"] for entry in result["results"]] == [1, 2, 3, 4]
    assert result["summary"]["queued"] == 2 and result["summary"]["invalid"] == 1
    assert result["pending_count"] == 2 and result["pending_limit"] == 10
    assert "private" not in json.dumps(result)
    jobs = [store.claim()]
    assert jobs[0]["url"] == URL and jobs[0]["video_limit_mib"] == 512 and jobs[0]["include_audio"] == 1
    store.fail(jobs[0]["id"], "INTERRUPTED")
    assert store.claim()["url"] == url(1)


@pytest.mark.parametrize("body,error", [
    ({"input": "no links"}, "INVALID_INPUT"),
    ({"input": ""}, "INVALID_INPUT"),
    ({"input": None}, "INVALID_INPUT"),
    ({"input": 1}, "INVALID_INPUT"),
    ({"input": "x" * 4097}, "INVALID_INPUT"),
    ({"input": " ".join([URL] * 11)}, "BATCH_LIMIT"),
    ({"input": URL, "include_audio": 1}, "INVALID_REQUEST"),
    ({"input": URL, "include_audio": "true"}, "INVALID_REQUEST"),
    ({"input": URL, "video_limit_mib": True}, "INVALID_INPUT"),
    ({"input": URL, "video_limit_mib": 8193}, "INVALID_INPUT"),
    ({"input": URL, "force_download": True}, "INVALID_REQUEST"),
    ({"input": URL, "supplement_duplicate_audio": True}, "INVALID_REQUEST"),
    ({}, "INVALID_REQUEST"),
])
def test_invalid_batch_envelope_never_adds_partial_tasks(batch_client, body, error):
    client, store, _ = batch_client
    response = post(client, **body)
    assert response.status_code == 400 and response.json()["error"] == error
    assert store.search()["total"] == 0


def test_queue_capacity_is_partial_and_duplicates_do_not_consume_slots(batch_client):
    client, store, _ = batch_client
    for index in range(8):
        store.create(url(index))
    result = post(client, input="\n".join(url(index) for index in (10, 11, 0, 12, 13))).json()
    assert [entry["status"] for entry in result["results"]] == ["queued", "queued", "already_queued", "queue_full", "queue_full"]
    assert result["results"][-1]["url"] == url(13)
    assert result["pending_count"] == 10 and store.search()["total"] == 10
    assert post(client, input=" ".join([url(0)] * 10)).json()["summary"]["already_queued"] == 10


@pytest.mark.parametrize("hidden", [False, True])
def test_saved_results_remain_owned_and_options_are_not_silently_changed(batch_client, media_fixtures, hidden):
    client, store, _ = batch_client
    saved, path, original = completed(store, media_fixtures[0])
    if hidden:
        store.remove(saved["id"])
    result = post(client, input=URL, include_audio=True).json()
    entry = result["results"][0]
    assert entry["status"] == "saved" and entry["existing"]["id"] == saved["id"]
    assert entry["existing"]["hidden"] is hidden
    assert result["pending_count"] == 0 and path.read_bytes() == original
    with store.connection() as db:
        row = db.execute("SELECT include_audio,audio_state,hidden_at FROM jobs WHERE id=?", (saved["id"],)).fetchone()
    assert row["include_audio"] == 0 and row["audio_state"] == "none"
    assert (row["hidden_at"] is not None) is hidden


def test_bad_destinations_are_per_item_and_never_echo_credentials(batch_client):
    client, store, _ = batch_client
    text = f"https://user:secret@example.com/private?token=secret https://127.0.0.1/video/7686430330920075249 {url(1)}%0d%0aevil {URL}"
    result = post(client, input=text).json()
    assert [entry["status"] for entry in result["results"]] == ["invalid", "invalid", "invalid", "queued"]
    assert all(entry["url"] is None for entry in result["results"][:3])
    assert "secret" not in json.dumps(result) and store.search()["total"] == 1


def test_batch_cancel_only_changes_pending_tasks_and_clears_waiting_audio(batch_client):
    client, store, _ = batch_client
    running = store.create(URL); store.claim()
    pending = store.create(url(1), include_audio=True)
    result = post(client, "/cancel", ids=[running["id"], pending["id"], "a" * 32]).json()
    assert [entry["status"] for entry in result["results"]] == ["not_pending", "cancelled", "not_found"]
    assert store.get(running["id"])["status"] == "running"
    assert store.get(pending["id"])["audio"]["status"] == "none"
    assert post(client, "/cancel", ids=[pending["id"]]).json()["results"][0]["status"] == "cancelled"


@pytest.mark.parametrize("action", ["/cancel", "/retry"])
@pytest.mark.parametrize("ids", [None, [], [1], ["../outside"], ["a" * 32] * 2, [f"{index:032x}" for index in range(11)]])
def test_batch_id_validation_is_atomic(batch_client, action, ids):
    client, store, _ = batch_client
    first = store.create(URL)
    if isinstance(ids, list) and ids:
        ids = [first["id"], *ids]
    response = post(client, action, ids=ids)
    assert response.status_code == 400
    assert store.get(first["id"])["status"] == "pending" and store.search()["total"] == 1


def test_retry_keeps_each_original_limit_and_audio_option(batch_client):
    client, store, _ = batch_client
    first = store.create(URL, 128, include_audio=True); store.fail(first["id"], "BLOCKED")
    second = store.create(url(1), 2048); store.cancel(second["id"])
    third = store.create(url(2), 64)
    with store.connection() as db:
        db.execute("UPDATE jobs SET status='expired' WHERE id=?", (third["id"],))
    response = post(client, "/retry", ids=[first["id"], second["id"], third["id"]])
    assert response.status_code == 202
    result = response.json()
    assert [entry["status"] for entry in result["results"]] == ["queued"] * 3
    jobs = [entry["job"] for entry in result["results"]]
    assert [job["video_limit_mib"] for job in jobs] == [128, 2048, 64]
    assert [job["include_audio"] for job in jobs] == [True, False, False]
    assert all(job["id"] != old["id"] for job, old in zip(jobs, (first, second, third)))


def test_retry_reports_changed_missing_and_duplicate_tasks(batch_client):
    client, store, _ = batch_client
    first = store.create(URL); store.fail(first["id"], "BLOCKED")
    second = store.create(URL); store.fail(second["id"], "BLOCKED")
    active = store.create(url(1))
    result = post(client, "/retry", ids=[first["id"], second["id"], active["id"], "a" * 32]).json()
    assert [entry["status"] for entry in result["results"]] == ["queued", "already_queued", "not_retryable", "not_found"]
    assert result["results"][1]["existing"]["id"] == result["results"][0]["job"]["id"]


@pytest.mark.parametrize("action,body", [("", {"input": URL}), ("/cancel", {"ids": ["a" * 32]}), ("/retry", {"ids": ["a" * 32]})])
def test_batch_routes_keep_origin_and_csrf_guards(batch_client, action, body):
    client, store, _ = batch_client
    path = "/api/jobs/batch" + action
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"Origin": "https://example.invalid", "X-CSRF-Token": headers(client)["X-CSRF-Token"]}).status_code == 403
    assert client.post(path, json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert store.search()["total"] == 0


def test_stopped_worker_rejects_new_jobs_but_pending_cancel_still_works(batch_client):
    client, store, worker = batch_client
    job = store.create(URL)
    worker.stopping.set()
    assert post(client, input=url(1)).status_code == 503
    assert post(client, "/retry", ids=[job["id"]]).status_code == 503
    assert post(client, "/cancel", ids=[job["id"]]).json()["results"][0]["status"] == "cancelled"


def test_parallel_batches_never_exceed_queue_capacity(web_config):
    from lumabox.web.batch import BatchJobs
    store = JobStore(web_config)
    barrier = threading.Barrier(2)
    def submit(start):
        barrier.wait()
        return BatchJobs(store).submit("\n".join(url(index) for index in range(start, start + 7)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (0, 20)))
    assert sum(result["summary"].get("queued", 0) for result in results) == 10
    assert sum(result["summary"].get("queue_full", 0) for result in results) == 4
    assert store.search()["total"] == 10


def test_claim_racing_bulk_cancel_never_cancels_a_started_download(web_config):
    from lumabox.web.batch import BatchJobs
    store = JobStore(web_config); job = store.create(URL)
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait(); return store.claim()
    def cancel():
        barrier.wait(); return BatchJobs(store).cancel([job["id"]])
    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming, cancelling = pool.submit(claim), pool.submit(cancel)
        claimed, result = claiming.result(), cancelling.result()
    if claimed:
        assert store.get(job["id"])["status"] == "running"
        assert result["results"][0]["status"] == "not_pending"
    else:
        assert store.get(job["id"])["status"] == "cancelled"


def test_batch_tasks_use_real_worker_and_late_short_link_deduplication(batch_client, tmp_path, media_fixtures):
    client, store, _ = batch_client
    result = post(client, input=URL + "\nhttps://v.douyin.com/another-link/", include_audio=True).json()
    first, second = [entry["job"] for entry in result["results"]]
    worker = fixture_worker(store.config, store, tmp_path, media_fixtures[0])
    assert worker.run_once() and store.get(first["id"])["status"] == "success"
    assert worker.run_once() and store.get(first["id"])["audio"]["status"] == "success"
    assert worker.run_once() and store.get(second["id"])["status"] == "duplicate"
    assert store.get(second["id"])["files"] == []
    assert not (store.config.output_dir / second["id"]).exists()


def test_retry_partial_capacity_and_second_request_reuse_the_same_new_task(batch_client):
    client, store, _ = batch_client
    originals = []
    for index in range(3):
        job = store.create(url(index), 64 + index, include_audio=bool(index % 2))
        store.fail(job["id"], "BLOCKED")
        originals.append(job)
    for index in range(10, 18):
        store.create(url(index))
    result = post(client, "/retry", ids=[job["id"] for job in originals]).json()
    assert [item["status"] for item in result["results"]] == ["queued", "queued", "queue_full"]
    again = post(client, "/retry", ids=[originals[0]["id"]]).json()["results"][0]
    assert again["status"] == "already_queued"
    assert again["existing"]["id"] == result["results"][0]["job"]["id"]
    assert all(store.get(job["id"])["status"] == "failed" for job in originals)


def test_retry_uses_available_saved_work_without_redownloading(batch_client, media_fixtures):
    client, store, _ = batch_client
    failed = store.create(URL, include_audio=True); store.fail(failed["id"], "BLOCKED")
    saved, path, original = completed(store, media_fixtures[0])
    result = post(client, "/retry", ids=[failed["id"]]).json()
    assert result["results"][0]["status"] == "saved"
    assert result["results"][0]["existing"]["id"] == saved["id"]
    assert result["pending_count"] == 0 and path.read_bytes() == original
    assert store.get(saved["id"])["audio"]["status"] == "none"


@pytest.mark.parametrize("action", ["submit", "retry"])
def test_late_short_link_duplicates_do_not_change_saved_audio_options(batch_client, media_fixtures, action):
    client, store, _ = batch_client
    short = "https://v.douyin.com/unknown-batch-link/"
    if action == "retry":
        failed = store.create(short, include_audio=True)
        store.fail(failed["id"], "BLOCKED")
    saved, path, original = completed(store, media_fixtures[0])
    if action == "submit":
        result = post(client, input=short, include_audio=True).json()
    else:
        result = post(client, "/retry", ids=[failed["id"]]).json()
    job = result["results"][0]["job"]
    store = JobStore(store.config)  # Keep the batch policy after a process restart.
    assert store.claim()["id"] == job["id"]
    assert store.mark_duplicate(job["id"], saved["id"])
    target = store.get(saved["id"])
    assert target["include_audio"] is False
    assert target["audio"]["status"] == "none"
    assert path.read_bytes() == original
    assert store.get(job["id"])["status"] == "duplicate"


def test_single_short_link_duplicate_still_supplements_requested_audio(batch_client, media_fixtures):
    _, store, _ = batch_client
    saved, _, _ = completed(store, media_fixtures[0])
    job = store.create("https://v.douyin.com/unknown-single-link/", include_audio=True)
    assert store.claim()["id"] == job["id"]
    assert store.mark_duplicate(job["id"], saved["id"])
    assert store.get(saved["id"])["include_audio"] is True
    assert store.get(saved["id"])["audio"]["status"] == "pending"


def test_pre_batch_database_keeps_legacy_audio_supplement_behavior(batch_client, media_fixtures):
    _, store, _ = batch_client
    saved, _, _ = completed(store, media_fixtures[0])
    job = store.create("https://v.douyin.com/legacy-link/", include_audio=True)
    with store.connection() as db:
        db.execute("ALTER TABLE jobs DROP COLUMN supplement_duplicate_audio")
    upgraded = JobStore(store.config)
    assert upgraded.claim()["id"] == job["id"]
    assert upgraded.mark_duplicate(job["id"], saved["id"])
    assert upgraded.get(saved["id"])["audio"]["status"] == "pending"


def test_batch_queue_survives_reopen_without_saving_share_text(batch_client):
    client, store, _ = batch_client
    result = post(client, input=f"私有分享文案 {URL}?tracking=secret\n{url(1)}", include_audio=True).json()
    reopened = JobStore(store.config)
    reopened.recover()
    active = reopened.active()
    assert [job["id"] for job in active] == [item["job"]["id"] for item in result["results"]]
    assert all(job["status"] == "pending" and job["include_audio"] for job in active)
    assert "secret" not in json.dumps(active)
    assert reopened.claim()["id"] == active[0]["id"]


@pytest.mark.parametrize("action", ["/cancel", "/retry"])
def test_action_envelopes_reject_extra_fields_before_mutation(batch_client, action):
    client, store, _ = batch_client
    job = store.create(URL)
    response = post(client, action, ids=[job["id"]], force_download=True)
    assert response.status_code == 400 and response.json()["error"] == "INVALID_REQUEST"
    assert store.get(job["id"])["status"] == "pending"


@pytest.mark.parametrize("action,body", [("", {"input": URL}), ("/cancel", {"ids": ["a" * 32]}), ("/retry", {"ids": ["a" * 32]})])
def test_cloud_batch_routes_require_login(web_config, action, body):
    from dataclasses import replace
    password_file = web_config.data_dir.parent / "test-access.json"
    password_file.write_text(json.dumps({"password": "fixture-password"}))
    config = replace(web_config, access_file=password_file)
    client = TestClient(create_app(config), base_url=ORIGIN)
    try:
        response = client.post("/api/jobs/batch" + action, json=body, headers={"Origin": ORIGIN})
        assert response.status_code == 401 and response.json()["error"] == "LOGIN_REQUIRED"
    finally:
        client.close()
