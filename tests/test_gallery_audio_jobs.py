"""Gallery music is optional native audio attached to an intact image collection."""
import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from lumabox.errors import LumaBoxError
from lumabox.web.config import WebError
from lumabox.web.store import JobStore
from test_audio_jobs import client_for
from test_daily_api import headers
from web_helpers import WORK_ID, fixture_worker


@pytest.fixture(scope="session")
def music_mp3(tmp_path_factory):
    path = tmp_path_factory.mktemp("gallery-audio") / "music.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.5", "-c:a", "libmp3lame", str(path)], check=True)
    return path


def saved_gallery(store, tmp_path, media_fixtures, *, selected=False):
    job = store.create("https://v.douyin.com/gallery/", include_audio=selected)
    assert fixture_worker(store.config, store, tmp_path, media_fixtures[1], "gallery").run_once()
    saved = store.get(job["id"])
    originals = {f["id"]: store.file_path(store.file(f["id"])).read_bytes() for f in saved["files"]}
    return saved, originals


def native_music(monkeypatch, payload, *, failure=None):
    import lumabox.web.audio as audio

    def download(work_id, target, *, headless, cancelled, max_bytes, on_created):
        assert work_id == WORK_ID
        assert not cancelled()
        if failure is not None:
            raise LumaBoxError(failure)
        with target.open("xb") as stream:
            stat = target.stat()
            on_created(target, (stat.st_dev, stat.st_ino))
            stream.write(payload)
        return {"name": target.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "mime": "audio/mpeg", "duration": .55}

    monkeypatch.setattr(audio, "download_gallery_music", download, raising=False)


def test_selected_gallery_schedules_music_after_images(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    saved, originals = saved_gallery(store, tmp_path, media_fixtures, selected=True)
    assert saved["status"] == "success" and saved["audio"]["status"] == "pending"
    assert store.active()[0]["id"] == saved["id"]
    assert originals


def test_native_mp3_supplement_preview_download_and_purge(web_config, tmp_path, media_fixtures, music_mp3, monkeypatch):
    store = JobStore(web_config)
    saved, originals = saved_gallery(store, tmp_path, media_fixtures)
    assert saved["audio"]["status"] == "none"
    payload = music_mp3.read_bytes()
    native_music(monkeypatch, payload)
    with client_for(store) as client:
        endpoint = f'/api/jobs/{saved["id"]}/audio'
        response = client.post(endpoint, headers=headers(client), json={})
        assert response.status_code == 202 and response.json()["audio"]["status"] == "pending"
        assert store.audio.run_once(threading.Event())
        complete = store.get(saved["id"])
        assert complete["audio"] == {"status": "success", "error": None}
        assert complete["actual_count"] == saved["actual_count"] == 1
        audio = next(f for f in complete["files"] if f["mime"] == "audio/mpeg")
        assert audio["name"].endswith(".mp3") and audio["bytes"] == len(payload)
        audio_path = store.file_path(store.file(audio["id"]))
        assert audio_path.read_bytes() == payload
        for file_id, data in originals.items():
            assert store.file_path(store.file(file_id)).read_bytes() == data
        assert len(complete["files"]) == len(originals) + 1
        part = client.get(f'/api/files/{audio["id"]}/preview', headers={"Range": "bytes=0-31"})
        assert part.status_code == 206 and part.content == payload[:32]
        assert part.headers["content-type"].startswith("audio/mpeg")
        assert client.get(f'/api/files/{audio["id"]}').content == payload
        assert client.post(endpoint, headers=headers(client), json={}).json()["audio"]["status"] == "success"
        assert not store.audio.run_once(threading.Event())
    assert store.storage.summary(refresh=True)["saved_bytes"] == sum(map(len, originals.values())) + len(payload)
    assert store.storage.purge(saved["id"])["status"] == "deleted"
    assert not audio_path.exists()


@pytest.mark.parametrize("error", ["NO_AUDIO", "VERIFY_FAILED", "TIMEOUT", "UNSAFE_TARGET"])
def test_music_failure_keeps_images_and_can_retry(web_config, tmp_path, media_fixtures, music_mp3, monkeypatch, error):
    store = JobStore(web_config)
    saved, originals = saved_gallery(store, tmp_path, media_fixtures, selected=True)
    native_music(monkeypatch, b"", failure=error)
    assert store.audio.run_once(threading.Event())
    failed = store.get(saved["id"])
    assert failed["status"] == "success" and failed["audio"] == {"status": "failed", "error": error}
    assert len(failed["files"]) == len(originals)
    native_music(monkeypatch, music_mp3.read_bytes())
    store.audio.request(saved["id"])
    assert store.audio.run_once(threading.Event())
    assert store.get(saved["id"])["audio"]["status"] == "success"


def test_missing_music_does_not_block_reuse_and_can_be_resaved(web_config, tmp_path, media_fixtures, music_mp3, monkeypatch):
    store = JobStore(web_config)
    saved, originals = saved_gallery(store, tmp_path, media_fixtures, selected=True)
    native_music(monkeypatch, music_mp3.read_bytes())
    assert store.audio.run_once(threading.Event())
    audio = next(f for f in store.get(saved["id"])["files"] if f["mime"] == "audio/mpeg")
    store.file_path(store.file(audio["id"])).unlink()
    assert store.get(saved["id"])["audio"]["error"] == "RESULT_UNAVAILABLE"
    with client_for(store) as client:
        response = client.post('/api/jobs', headers=headers(client), json={"input": "https://v.douyin.com/gallery/"})
        assert response.status_code == 409 and response.json()["existing"]["id"] == saved["id"]
    store.audio.request(saved["id"])
    assert store.audio.run_once(threading.Event())
    assert store.get(saved["id"])["audio"]["status"] == "success"
    assert len(store.get(saved["id"])["files"]) == len(originals) + 1


def test_gallery_cancel_cleans_owned_mp3_and_preserves_images(web_config, tmp_path, media_fixtures, monkeypatch):
    import lumabox.web.audio as audio
    store = JobStore(web_config)
    saved, originals = saved_gallery(store, tmp_path, media_fixtures, selected=True)
    entered, release = threading.Event(), threading.Event()

    def blocked(work_id, target, *, on_created, cancelled, **kwargs):
        with target.open("xb") as stream:
            stat = target.stat()
            on_created(target, (stat.st_dev, stat.st_ino))
            stream.write(b"partial")
        entered.set()
        assert release.wait(5)
        assert cancelled()
        raise LumaBoxError("INTERRUPTED")

    monkeypatch.setattr(audio, "download_gallery_music", blocked, raising=False)
    thread = threading.Thread(target=store.audio.run_once, args=(threading.Event(),))
    thread.start()
    try:
        assert entered.wait(5)
        assert store.downloads[saved["id"]] == 1
        store.audio.cancel(saved["id"])
        with pytest.raises(WebError, match="JOB_ACTIVE"):
            store.storage.purge(saved["id"])
    finally:
        release.set()
        thread.join(8)
    assert not thread.is_alive()
    assert store.get(saved["id"])["audio"] == {"status": "cancelled", "error": None}
    for file_id, data in originals.items():
        assert store.file_path(store.file(file_id)).read_bytes() == data
    assert not list(web_config.output_dir.rglob("*.mp3"))


def test_process_tree_separates_binary_stdout_and_bounded_status(tmp_path):
    from lumabox.web.process import ProcessTree
    with (tmp_path / "audio").open("w+b") as output, (tmp_path / "status").open("w+b") as status:
        with ProcessTree([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'ID3\\x00');sys.stderr.write('{\"ok\":true}')"], output, stderr=status) as tree:
            assert tree.process.wait(timeout=10) == 0
        output.seek(0); status.seek(0)
        assert output.read() == b"ID3\0"
        assert json.loads(status.read()) == {"ok": True}


@pytest.mark.parametrize("state", ["pending", "running", "cancelling", "success"])
def test_gallery_mp3_migration_and_recovery_preserve_images(web_config, tmp_path, tmp_path_factory, media_fixtures, music_mp3, monkeypatch, state):
    # Keep the temporary migration directory below legacy Windows MAX_PATH;
    # generated attachment names include both work identity and ownership token.
    short_root = tmp_path_factory.mktemp("gm")
    web_config = replace(web_config, data_dir=short_root / "d", output=short_root / "o")
    store = JobStore(web_config)
    store.migrate_outputs()
    saved, originals = saved_gallery(store, tmp_path, media_fixtures, selected=True)
    native_music(monkeypatch, music_mp3.read_bytes())
    partial = None
    if state == "success":
        assert store.audio.run_once(threading.Event())
    elif state != "pending":
        token = "c" * 32
        first = store.file_path(store.file(next(iter(originals))))
        partial = first.parent / f".audio-{token}.mp3"
        partial.write_bytes(b"partial")
        stat = partial.stat()
        manifest = [{"path": str(partial.relative_to(web_config.output_dir)), "device": stat.st_dev, "inode": stat.st_ino}]
        with store.connection() as db:
            db.execute("UPDATE jobs SET audio_state=?,audio_token=?,audio_root=?,audio_manifest=? WHERE id=?",
                       (state, token, str(web_config.output_dir), json.dumps(manifest), saved["id"]))
    moved = JobStore(replace(web_config, output=short_root / "m"))
    moved.migrate_outputs(); moved.recover()
    if partial is not None:
        assert not partial.exists()
    for file_id, data in originals.items():
        assert moved.file_path(moved.file(file_id)).read_bytes() == data
        assert store.file_path(store.file(file_id)).read_bytes() == data
    if state in ("running", "cancelling"):
        moved.audio.request(saved["id"])
    if state != "success":
        assert moved.audio.run_once(threading.Event())
    assert moved.get(saved["id"])["audio"]["status"] == "success"
    assert moved.storage.purge(saved["id"])["status"] == "deleted"


@pytest.mark.parametrize("supplement", [False, True])
def test_late_gallery_duplicate_respects_single_or_batch_audio_policy(web_config, tmp_path, media_fixtures, supplement):
    store = JobStore(web_config)
    saved, originals = saved_gallery(store, tmp_path, media_fixtures)
    job = store.create("https://v.douyin.com/other-gallery/", include_audio=True,
                       supplement_duplicate_audio=supplement)
    assert store.claim()["id"] == job["id"]
    assert store.mark_duplicate(job["id"], saved["id"])
    assert store.get(saved["id"])["audio"]["status"] == ("pending" if supplement else "none")
    for file_id, data in originals.items():
        assert store.file_path(store.file(file_id)).read_bytes() == data
