import json
import os
import subprocess
import sys
from pathlib import Path
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
    return WebConfig(tmp_path / "data", capacity_bytes=2 * 1024**3, retention_seconds=86400)


def test_queue_limit_fifo_and_running_is_not_pending(config):
    store = JobStore(config)
    first = store.create(URL)
    assert store.claim()["id"] == first["id"]
    queued = [store.create(f"https://www.douyin.com/video/{7686430330920075250 + i}") for i in range(10)]
    with pytest.raises(WebError, match="QUEUE_FULL"):
        store.create("https://www.douyin.com/video/7686430330920075269")
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
    waiting = store.create("https://v.douyin.com/following/")
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
    assert store.file_path(file).parent == Path(result["output_directory"])
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
    pending = store.create(URL, force_download=True)
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


def test_download_lease_defers_expiry_until_stream_closes(config, tmp_path, media_fixtures):
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    file_id = store.get(job["id"])["files"][0]["id"]
    with store.open_file(file_id) as (file, stream):
        store.expire(now=time.time() + 86401)
        assert store.get(job["id"])["status"] == "success"
        assert stream.read() == media_fixtures[0].read_bytes()
    store.expire(now=time.time() + 86401)
    assert store.get(job["id"])["status"] == "expired"


def test_cleanup_failure_is_retried_without_stranding_queue(config, tmp_path, media_fixtures, monkeypatch):
    store = JobStore(config)
    old = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    future = time.time() + 86401
    original_cleanup = store.cleanup
    attempts = []
    def temporarily_locked(job_id):
        attempts.append(job_id)
        if job_id == old["id"]:
            raise PermissionError("controlled sharing violation")
        original_cleanup(job_id)
    monkeypatch.setattr(store, "cleanup", temporarily_locked)
    store.expire(now=future)
    next_job = store.create(URL, force_download=True)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    assert store.get(next_job["id"])["status"] == "success"
    monkeypatch.setattr(store, "cleanup", original_cleanup)
    store.expire(now=future)
    assert store.get(old["id"])["status"] == "expired"
    assert attempts == [old["id"]]


def test_failure_terminal_state_does_not_depend_on_cleanup(config, tmp_path, media_fixtures, monkeypatch):
    store = JobStore(config)
    job = store.create(URL)
    def locked(_):
        raise PermissionError("controlled sharing violation")
    monkeypatch.setattr(store, "cleanup", locked)
    fixture_worker(config, store, tmp_path, media_fixtures[0], "failed").run_once()
    assert store.get(job["id"])["status"] == "failed"
    assert store.get(job["id"])["error"] == "AUTH_REQUIRED"


def test_auxiliary_log_delete_failure_never_removes_published_media(config, tmp_path, media_fixtures, monkeypatch):
    from pathlib import Path
    original_unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path.name == "cli-result.jsonl":
            raise PermissionError("controlled sharing violation")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked)
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    result = store.get(job["id"])
    assert result["status"] == "success"
    assert store.file_path(store.file(result["files"][0]["id"])).read_bytes() == media_fixtures[0].read_bytes()


def test_gallery_rewrite_does_not_hold_two_archives(config, tmp_path, media_fixtures, monkeypatch):
    store = JobStore(config)
    job = store.create("https://v.douyin.com/test/")
    original_write = zipfile.ZipFile.write
    def measure(archive, filename, *args, **kwargs):
        # Only the real rewrite in this process is observed; child creates the original ZIP.
        existing = list((config.output_dir / job["id"]).rglob("*.zip"))
        assert not existing, "Old ZIP still takes space while replacement is written"
        return original_write(archive, filename, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "write", measure)
    fixture_worker(config, store, tmp_path, media_fixtures[1], "gallery").run_once()
    assert store.get(job["id"])["status"] == "success"


@pytest.mark.parametrize("code", ["CHALLENGE_REQUIRED", "RESOURCE_TIMEOUT"])
def test_core_access_errors_keep_actionable_fixed_codes(config, tmp_path, media_fixtures, code):
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0], code).run_once()
    assert store.get(job["id"])["error"] == code


@pytest.mark.skipif(os.name != "nt", reason="Windows lifecycle regression")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_process_tree_stops_real_ignoring_child_and_grandchild(tmp_path, parent_exits):
    import ctypes
    from ctypes import wintypes
    from lumabox.web.process import ProcessTree
    marker = tmp_path / "pids.txt"
    leaf = "import os,signal,sys,time;from pathlib import Path;signal.signal(signal.SIGBREAK,signal.SIG_IGN);Path(sys.argv[1]).open('a').write(str(os.getpid())+'\\n');time.sleep(60)"
    middle = "import os,subprocess,sys,time,signal;from pathlib import Path;signal.signal(signal.SIGBREAK,signal.SIG_IGN);Path(sys.argv[1]).open('a').write(str(os.getpid())+'\\n');subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]);time.sleep(60)"
    parent = "import subprocess,sys,time,signal;signal.signal(signal.SIGBREAK,signal.SIG_IGN);subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1],sys.argv[3]]);time.sleep(.6 if sys.argv[4]=='exit' else 60)"
    with (tmp_path / "out.log").open("wb") as output:
        with ProcessTree([sys.executable, "-c", parent, str(marker), middle, leaf, "exit" if parent_exits else "wait"], output) as tree:
            deadline = time.monotonic() + 5
            while (not marker.exists() or len(marker.read_text().splitlines()) < 2) and time.monotonic() < deadline:
                time.sleep(.02)
            assert marker.exists() and len(marker.read_text().splitlines()) == 2
            if parent_exits:
                tree.process.wait(timeout=5)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handles = [kernel.OpenProcess(0x100000, False, int(pid)) for pid in marker.read_text().splitlines()]
            try:
                assert all(handles)
                tree.stop()
                assert tree.process.poll() is not None
                assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles)
            finally:
                for handle in handles:
                    if handle: kernel.CloseHandle(handle)


def test_process_tree_allows_real_chromium_launch_and_render(tmp_path):
    from lumabox.web.process import ProcessTree
    script = tmp_path / "browser.py"
    script.write_text('''from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<h1>nested browser rendered</h1>')
    print(page.locator('h1').inner_text())
    browser.close()
''', encoding="utf-8")
    output_path = tmp_path / "browser.log"
    with output_path.open("wb") as output:
        with ProcessTree([sys.executable, str(script)], output) as tree:
            assert tree.process.wait(timeout=20) == 0
    assert output_path.read_text().strip() == "nested browser rendered"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_windows_junction_is_rejected_without_deleting_target(config, tmp_path, media_fixtures):
    import _winapi
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    file = store.file(store.get(job["id"])["files"][0]["id"])
    job_root = config.output_dir / job["id"]
    outside = tmp_path / "moved"
    job_root.rename(outside)
    _winapi.CreateJunction(str(outside), str(job_root))
    try:
        with pytest.raises(WebError, match="NOT_FOUND"):
            store.file_path(file)
        assert not store.try_cleanup(job["id"])
        assert list(outside.rglob("*.mp4"))[0].read_bytes() == media_fixtures[0].read_bytes()
    finally:
        job_root.rmdir()


def test_max_gallery_stays_below_capacity_at_reserve_boundary(config, tmp_path, media_fixtures, monkeypatch):
    store = JobStore(config)
    occupied = config.output_dir / "occupied.bin"
    with occupied.open("wb") as file:
        file.truncate(1536 * 1024**2)  # Independent boundary: 2 GiB minus 512 MiB.
    job = store.create("https://v.douyin.com/test/", video_limit_mib=100)
    peaks = []
    original_write = zipfile.ZipFile.write
    def measure(archive, filename, *args, **kwargs):
        result = original_write(archive, filename, *args, **kwargs)
        peak = store.usage()
        peaks.append(peak)
        assert peak <= 2 * 1024**3
        return result
    monkeypatch.setattr(zipfile.ZipFile, "write", measure)
    try:
        fixture_worker(config, store, tmp_path, media_fixtures[1], "gallery_large").run_once()
        assert store.get(job["id"])["status"] == "success"
        assert len(peaks) == 10
        assert max(peaks) < 2 * 1024**3
    finally:
        occupied.unlink()
        store.cleanup(job["id"])


def test_default_local_files_do_not_expire(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "data")
    store = JobStore(config)
    job = store.create(URL)
    fixture_worker(config, store, tmp_path, media_fixtures[0]).run_once()
    store.expire(now=time.time() + 365 * 86400)
    assert store.get(job["id"])["status"] == "success"
    assert store.file_path(store.file(store.get(job["id"])["files"][0]["id"])).read_bytes() == media_fixtures[0].read_bytes()


def test_legacy_success_copies_into_output_without_deleting_original(tmp_path, media_fixtures):
    legacy_config = WebConfig(tmp_path / "data", output=tmp_path / "data" / "outputs")
    legacy = JobStore(legacy_config)
    job = legacy.create(URL)
    fixture_worker(legacy_config, legacy, tmp_path, media_fixtures[0]).run_once()
    file_id = legacy.get(job["id"])["files"][0]["id"]
    original = legacy.file_path(legacy.file(file_id))
    config = WebConfig(tmp_path / "data")
    reopened = JobStore(config)
    reopened.migrate_outputs()
    migrated = reopened.file_path(reopened.file(file_id))
    assert migrated.is_relative_to(tmp_path / "Output")
    assert migrated.read_bytes() == original.read_bytes() == media_fixtures[0].read_bytes()
    reopened.migrate_outputs()
    assert migrated.read_bytes() == media_fixtures[0].read_bytes()


def test_unavailable_previous_output_preserves_mapping_and_resumes_after_reconnect(tmp_path, media_fixtures):
    legacy_config = WebConfig(tmp_path / "data", output=tmp_path / "data" / "outputs")
    legacy = JobStore(legacy_config)
    legacy.migrate_outputs()
    job = legacy.create(URL)
    fixture_worker(legacy_config, legacy, tmp_path, media_fixtures[0]).run_once()
    file_id = legacy.get(job["id"])["files"][0]["id"]
    offline = tmp_path / "offline"
    legacy_config.output_dir.rename(offline)
    config = WebConfig(tmp_path / "data")
    reopened = JobStore(config)
    with pytest.raises(WebError, match="MIGRATION_FAILED"):
        reopened.migrate_outputs()
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='output_root'").fetchone()[0] == str(legacy_config.output_dir)
    assert reopened.get(job["id"])["status"] == "success"
    assert list(offline.rglob("*.mp4"))[0].read_bytes() == media_fixtures[0].read_bytes()
    offline.rename(legacy_config.output_dir)
    resumed = JobStore(config)
    resumed.migrate_outputs()
    assert resumed.file_path(resumed.file(file_id)).read_bytes() == media_fixtures[0].read_bytes()


def test_output_nested_in_previous_job_is_rejected_before_copy(tmp_path, media_fixtures, monkeypatch):
    legacy_config = WebConfig(tmp_path / "data", output=tmp_path / "data" / "outputs")
    legacy = JobStore(legacy_config)
    legacy.migrate_outputs()
    job = legacy.create(URL)
    fixture_worker(legacy_config, legacy, tmp_path, media_fixtures[0]).run_once()
    original = legacy.file_path(legacy.file(legacy.get(job["id"])["files"][0]["id"]))
    config = WebConfig(tmp_path / "data", output=legacy_config.output_dir / job["id"] / "nested")
    reopened = JobStore(config)
    def never_copy(*args, **kwargs):
        raise AssertionError("Recursive source/destination copying must not begin")
    monkeypatch.setattr("lumabox.web.store.shutil.copytree", never_copy)
    with pytest.raises(WebError, match="MIGRATION_FAILED"):
        reopened.migrate_outputs()
    assert original.read_bytes() == media_fixtures[0].read_bytes()
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='output_root'").fetchone()[0] == str(legacy_config.output_dir)


def test_migration_rejects_same_length_corruption_in_staging(tmp_path, media_fixtures, monkeypatch):
    import shutil
    legacy_config = WebConfig(tmp_path / "data", output=tmp_path / "data" / "outputs")
    legacy = JobStore(legacy_config)
    legacy.migrate_outputs()
    job = legacy.create(URL)
    fixture_worker(legacy_config, legacy, tmp_path, media_fixtures[0]).run_once()
    original = legacy.file_path(legacy.file(legacy.get(job["id"])["files"][0]["id"]))
    copytree = shutil.copytree
    def corrupted_copy(source, destination, *args, **kwargs):
        result = copytree(source, destination, *args, **kwargs)
        if Path(source).name == job["id"]:
            copied = list(Path(destination).rglob("*.mp4"))[0]
            data = copied.read_bytes()
            copied.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        return result
    monkeypatch.setattr("lumabox.web.store.shutil.copytree", corrupted_copy)
    config = WebConfig(tmp_path / "data")
    reopened = JobStore(config)
    with pytest.raises(WebError, match="MIGRATION_FAILED"):
        reopened.migrate_outputs()
    assert original.read_bytes() == media_fixtures[0].read_bytes()
    assert not (config.output_dir / job["id"]).exists()
    assert not list(config.output_dir.glob(".migration-*"))
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='output_root'").fetchone()[0] == str(legacy_config.output_dir)


def test_migration_rejects_file_record_outside_its_job(tmp_path, media_fixtures):
    legacy_config = WebConfig(tmp_path / "data", output=tmp_path / "data" / "outputs")
    legacy = JobStore(legacy_config)
    legacy.migrate_outputs()
    job = legacy.create(URL)
    fixture_worker(legacy_config, legacy, tmp_path, media_fixtures[0]).run_once()
    misplaced = legacy_config.output_dir / "other" / "video.mp4"
    misplaced.parent.mkdir()
    misplaced.write_bytes(media_fixtures[0].read_bytes())
    with legacy.connection() as db:
        db.execute("UPDATE files SET relative_path=? WHERE job_id=?", ("other/video.mp4", job["id"]))
    config = WebConfig(tmp_path / "data")
    reopened = JobStore(config)
    with pytest.raises(WebError, match="MIGRATION_FAILED"):
        reopened.migrate_outputs()
    assert misplaced.read_bytes() == media_fixtures[0].read_bytes()
    assert not (config.output_dir / job["id"]).exists()
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='output_root'").fetchone()[0] == str(legacy_config.output_dir)


def test_failure_keeps_only_fixed_size_details_and_work_identity(config):
    store = JobStore(config)
    job = store.create("https://v.douyin.com/test/", video_limit_mib=1)
    store.fail(job["id"], "SIZE_LIMIT", {"work_id": WORK_ID, "kind": "video",
        "diagnostics": {"limit_bytes": 1048576, "actual_bytes": 2097152, "reason": "Cookie=secret"}})
    result = store.get(job["id"])
    assert result["work_id"] == WORK_ID and result["kind"] == "video"
    assert result["error_detail"] == {"limit_bytes": 1048576, "actual_bytes": 2097152}
    assert "secret" not in json.dumps(result)


def test_video_above_old_100_and_200_mib_limits_is_published(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "data")
    store = JobStore(config)
    job = store.create(URL)
    try:
        fixture_worker(config, store, tmp_path, media_fixtures[0], "video_large").run_once()
        result = store.get(job["id"])
        assert result["status"] == "success"
        assert result["files"][0]["bytes"] == 210763776
    finally:
        store.cleanup(job["id"])
