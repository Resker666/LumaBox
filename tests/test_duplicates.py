from concurrent.futures import ThreadPoolExecutor
import pytest
from lumabox.web.store import JobStore
from lumabox.web.config import WebError
from test_daily_api import completed, routed_client, headers
from web_helpers import URL, WORK_ID, fixture_worker


def test_active_work_cannot_be_forced_twice(web_config):
    from lumabox.web.duplicates import DuplicateWorkError
    store = JobStore(web_config); first = store.create(URL)
    with pytest.raises(DuplicateWorkError) as failure:
        store.create(URL + "?tracking=private", force_download=True)
    assert failure.value.code == "ALREADY_DOWNLOADING"
    assert failure.value.existing["id"] == first["id"]


def test_concurrent_submissions_only_create_one_job(web_config):
    from lumabox.web.duplicates import DuplicateWorkError
    store = JobStore(web_config)
    def submit(_):
        try:
            return store.create(URL)["id"]
        except DuplicateWorkError as error:
            return error.existing["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(8)))
    assert len(set(ids)) == 1 and store.search()["total"] == 1


def test_saved_work_reused_hidden_restored_or_explicitly_downloaded(web_config, media_fixtures):
    from lumabox.web.duplicates import DuplicateWorkError
    store = JobStore(web_config); original, path, data = completed(store, media_fixtures[0])
    store.remove(original["id"])
    with pytest.raises(DuplicateWorkError) as failure:
        store.create(URL)
    assert failure.value.existing["hidden"] is True
    assert store.restore(original["id"])["status"] == "success"
    newer = store.create(URL, force_download=True)
    assert newer["id"] != original["id"] and path.read_bytes() == data


def test_hidden_missing_media_does_not_block_new_download(web_config, media_fixtures):
    store = JobStore(web_config); old, path, _ = completed(store, media_fixtures[0])
    store.remove(old["id"]); path.unlink()
    assert store.create(URL)["status"] == "pending"
    with pytest.raises(WebError, match="RESULT_UNAVAILABLE"):
        store.restore(old["id"])


def test_duplicate_after_last_progress_is_not_published(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config); old, path, data = completed(store, media_fixtures[0])
    new = store.create("https://v.douyin.com/another-short-link/")
    fixture_worker(web_config, store, tmp_path, media_fixtures[0]).run_once()
    current = store.get(new["id"])
    assert current["status"] == "duplicate" and current["duplicate_of"] == old["id"]
    assert current["files"] == [] and not (web_config.output_dir / new["id"]).exists()
    assert path.read_bytes() == data


def test_cancel_wins_over_late_duplicate(web_config, media_fixtures):
    store = JobStore(web_config); old, _, _ = completed(store, media_fixtures[0])
    new = store.create("https://v.douyin.com/another-short-link/"); store.claim()
    store.cancel(new["id"])
    assert not store.mark_duplicate(new["id"], old["id"])
    store.fail(new["id"], "INTERRUPTED")
    assert store.get(new["id"])["status"] == "cancelled"


def test_identity_and_kind_must_match_for_reuse(web_config, media_fixtures):
    store = JobStore(web_config); completed(store, media_fixtures[0])
    assert store.create(f"https://www.douyin.com/note/{WORK_ID}")["status"] == "pending"


def test_duplicate_response_and_restore_are_guarded(web_config, media_fixtures):
    store = JobStore(web_config); old, _, _ = completed(store, media_fixtures[0]); store.remove(old["id"])
    with routed_client(web_config) as client:
        response = client.post("/api/jobs", json={"input": URL}, headers=headers(client))
        assert response.status_code == 409
        detail = response.json()
        assert detail["error"] == "DUPLICATE_WORK" and detail["existing"]["id"] == old["id"]
        assert "relative_path" not in str(detail) and "sha256" not in str(detail)
        route = f"/api/jobs/{old['id']}/restore"
        assert client.post(route, json={}).status_code == 403
        assert client.post(route, json={}, headers=headers(client)).json()["id"] == old["id"]
        assert client.post("/api/jobs", json={"input": URL, "force_download": "yes"}, headers=headers(client)).status_code == 400


def test_duplicate_target_tracks_visibility_and_deleted_files(web_config, media_fixtures):
    store = JobStore(web_config); old, path, data = completed(store, media_fixtures[0])
    duplicate = store.create("https://v.douyin.com/late-target/"); store.claim()
    assert store.mark_duplicate(duplicate["id"], old["id"])
    assert store.get(duplicate["id"])["duplicate_target"] == {"id": old["id"], "hidden": False}
    store.remove(old["id"])
    assert store.get(duplicate["id"])["duplicate_target"] == {"id": old["id"], "hidden": True}
    path.unlink()
    assert store.get(duplicate["id"])["duplicate_target"] is None
    path.write_bytes(data)
    assert store.storage.purge(old["id"])["status"] == "deleted"
    assert store.get(duplicate["id"])["duplicate_target"] is None
