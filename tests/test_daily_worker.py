"""Progress observation through real local children and the publication guard."""
import json
import threading
import time
from pathlib import Path
import os
import subprocess

import pytest

from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from web_helpers import CHILD, FixtureWorker, URL


class RecordingStore(JobStore):
    def __init__(self, config):
        super().__init__(config)
        self.observed = []

    def update_progress(self, job_id, snapshot):
        updated = super().update_progress(job_id, snapshot)
        if updated:
            self.observed.append((self.get(job_id)["status"], dict(snapshot)))
        return updated


def progress_worker(tmp_path, config, store, fixture, mode="success"):
    source = CHILD.replace("root=Path(root)", '''root=Path(root)
from lumabox.progress import ProgressFile
progress=ProgressFile(root/'.progress.json')
progress.emit(stage='media_transfer',work_id='7686430330920075249',kind='video',title='  每日标题  ',
    bytes_received=3,bytes_total=Path(fixture).stat().st_size,expected_count=1,resource_index=1)
time.sleep(.7)
progress.emit(stage='media_verification',bytes_received=Path(fixture).stat().st_size)
time.sleep(.4)''')
    source = source.replace("(dest/'report.json').write_text", "result['title']='每日标题'\n(dest/'report.json').write_text")
    script = tmp_path / "progress_child.py"
    script.write_text(source, encoding="utf-8")
    return FixtureWorker(config, store, script, fixture, mode)


def test_real_child_progress_visible_while_running_then_verified_success(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "WebData")
    store = RecordingStore(config)
    job = store.create(URL)
    worker = progress_worker(tmp_path, config, store, media_fixtures[0])
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.get(job["id"])
        if current.get("progress", {}).get("bytes_received") == 3:
            break
        time.sleep(.02)
    thread.join(8)
    assert not thread.is_alive()
    assert current["status"] == "running"
    assert current["title"] == "每日标题"
    assert current["progress"]["bytes_received"] == 3
    final = store.get(job["id"])
    assert final["status"] == "success"
    assert final["title"] == "每日标题"
    assert final["progress"]["stage"] == "publication"
    assert final["progress"]["completed_count"] == 1
    assert final["progress"]["bytes_received"] == media_fixtures[0].stat().st_size
    assert final["files"]
    assert all(status == "running" for status, _ in store.observed)
    assert not (config.output_dir / job["id"] / ".progress.json").exists()


def test_progress_never_promotes_a_forged_child_report(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "WebData")
    store = RecordingStore(config)
    job = store.create(URL)
    progress_worker(tmp_path, config, store, media_fixtures[0], "hash").run_once()
    final = store.get(job["id"])
    assert final["status"] == "failed"
    assert final["files"] == []
    assert store.observed and all(status == "running" for status, _ in store.observed)


def test_worker_progress_argument_stays_inside_job_output(tmp_path):
    config = WebConfig(tmp_path / "WebData")
    store = JobStore(config)
    job = store.create(URL)
    from lumabox.web.worker import JobWorker
    directory = config.output_dir / job["id"]
    command = JobWorker(config, store).command(store.claim(), directory)
    assert command[command.index("--progress-file") + 1] == str(directory / ".progress.json")


def test_private_progress_cleanup_failure_keeps_published_media(tmp_path, media_fixtures, monkeypatch):
    config = WebConfig(tmp_path / "WebData")
    store = RecordingStore(config)
    job = store.create(URL)
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == ".progress.json":
            raise PermissionError("private file busy")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    progress_worker(tmp_path, config, store, media_fixtures[0]).run_once()
    final = store.get(job["id"])
    assert final["status"] == "success"
    assert store.file_path(store.file(final["files"][0]["id"])).read_bytes() == media_fixtures[0].read_bytes()


def test_malformed_child_snapshot_does_not_block_verified_success(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "WebData")
    store = RecordingStore(config)
    job = store.create(URL)
    source = CHILD.replace("root=Path(root)", "root=Path(root)\n(root/'.progress.json').write_bytes(b'{'*20000)\ntime.sleep(.2)")
    script = tmp_path / "malformed_child.py"
    script.write_text(source, encoding="utf-8")
    FixtureWorker(config, store, script, media_fixtures[0]).run_once()
    final = store.get(job["id"])
    assert final["status"] == "success"
    assert final["progress"]["stage"] == "publication"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory reparse guard")
def test_worker_progress_never_reads_through_job_directory_junction(tmp_path):
    from lumabox.web.worker import JobWorker
    config = WebConfig(tmp_path / "WebData")
    store = JobStore(config)
    job = store.create(URL)
    outside = tmp_path / "outside"
    outside.mkdir()
    snapshot = outside / ".progress.json"
    snapshot.write_text(json.dumps({"stage": "browser_start", "title": "private outside metadata"}), encoding="utf-8")
    junction = config.output_dir / job["id"]
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    try:
        assert JobWorker(config, store)._read_progress(junction / ".progress.json") is None
        assert snapshot.read_text(encoding="utf-8").find("private outside metadata") != -1
    finally:
        junction.rmdir()
