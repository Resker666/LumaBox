import asyncio
import json
import shutil
import threading
import zipfile
from pathlib import Path

import pytest
import lumabox.runner as runner
from lumabox.models import Work, MediaResource

ID = "7695689642767235187"


@pytest.fixture
def pipeline(monkeypatch, media_fixtures):
    class Session:
        http_status = 200
        def __init__(self, headless):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def load(self, link):
            if link.kind == "video":
                return Work(link.work_id, "video", (MediaResource("https://v1.douyinvod.com/a.mp4", 1),))
            return Work(ID, "gallery", (MediaResource("https://p3.douyinpic.com/a.jpeg", 1), MediaResource("https://p3.douyinpic.com/a.jpeg", 2)))
        async def media_headers(self, url):
            return {}
    def save(resource, path, headers, max_bytes, cancel=None):
        headers(resource.url)
        source = media_fixtures[0 if path.suffix == ".mp4" else 1]
        shutil.copyfile(source, path)
        return path.stat().st_size
    monkeypatch.setattr(runner, "BrowserSession", Session)
    monkeypatch.setattr(runner, "download_resource", save)
    return save


async def test_gallery_publishes_ordered_files_and_zip(tmp_path, pipeline):
    result = await runner.run_download(f"https://www.douyin.com/note/{ID}", tmp_path, True)
    assert result["status"] == "success"
    assert result["actual_count"] == 2
    directory = Path(result["output_directory"])
    assert (directory / "01.jpg").read_bytes() == (directory / "02.jpg").read_bytes()
    with zipfile.ZipFile(directory / f"{ID}-images.zip") as archive:
        assert archive.namelist() == ["01.jpg", "02.jpg", "report.json"]
        assert archive.testzip() is None
    assert not list(tmp_path.glob(".partial-*"))


async def test_video_is_verified_before_success(tmp_path, pipeline):
    result = await runner.run_download("https://www.douyin.com/video/7686430330920075249", tmp_path, True)
    assert result["status"] == "success"
    assert result["files"][0]["width"] == 64
    assert result["checks"]["full_decode"] is True


@pytest.mark.parametrize("failure", ["download", "verify", "zip", "size"])
async def test_failures_never_publish_half_gallery(tmp_path, pipeline, monkeypatch, failure):
    previous = tmp_path / "previous.mp4"
    previous.write_bytes(b"previous result")
    if failure == "download":
        def bad_download(resource, path, *args):
            if resource.order == 2:
                raise OSError("signature=private Cookie=secret")
            return pipeline(resource, path, *args)
        monkeypatch.setattr(runner, "download_resource", bad_download)
    elif failure == "verify":
        monkeypatch.setattr(runner, "verify_file", lambda *args: (_ for _ in ()).throw(OSError("secret")))
    elif failure == "zip":
        monkeypatch.setattr(runner, "make_zip", lambda *args: (_ for _ in ()).throw(OSError("secret")))
    else:
        monkeypatch.setattr(runner, "GALLERY_LIMIT", 1)
    result = await runner.run_download(f"https://www.douyin.com/note/{ID}", tmp_path, True)
    assert result["status"] == "failed"
    assert result["actual_count"] == 0
    assert list(tmp_path.glob("*/02.jpg")) == []
    assert previous.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".partial-*"))
    assert "secret" not in json.dumps(result) and "signature=" not in json.dumps(result)
    assert len(list(tmp_path.glob("*-failed.json"))) == 1


async def test_cancellation_waits_for_worker_then_cleans(tmp_path, pipeline, monkeypatch):
    started = threading.Event()
    stopped = threading.Event()
    def wait_for_cancel(resource, path, headers, max_bytes, cancel=None):
        path.write_bytes(b"partial")
        started.set()
        assert cancel.wait(3)
        stopped.set()
        raise OSError("cancelled")
    monkeypatch.setattr(runner, "download_resource", wait_for_cancel)
    task = asyncio.create_task(runner.run_download(f"https://www.douyin.com/note/{ID}", tmp_path, True))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    result = await task
    assert stopped.is_set()
    assert result["error"] == "INTERRUPTED"
    assert not list(tmp_path.glob(".partial-*"))


async def test_report_write_failure_still_returns_non_success(tmp_path, monkeypatch):
    file = tmp_path / "file"
    file.write_bytes(b"cannot be a directory")
    result = await runner.run_download("bad input", file, True)
    assert result["status"] == "failed"
    assert result["report_write_failed"] is True


async def test_adjustable_video_limit_reaches_transfer_and_reports_limit(tmp_path, pipeline, monkeypatch):
    observed = []
    def save(resource, path, headers, max_bytes, cancel=None):
        observed.append(max_bytes)
        return pipeline(resource, path, headers, max_bytes, cancel)
    monkeypatch.setattr(runner, "download_resource", save)
    result = await runner.run_download("https://www.douyin.com/video/7686430330920075249", tmp_path, True, video_limit_mib=2048)
    assert result["status"] == "success"
    assert observed == [2147483648]
    assert result["limits"]["video_bytes"] == 2147483648


async def test_video_size_failure_preserves_actual_size_for_ui(tmp_path, pipeline, monkeypatch):
    from lumabox.errors import LumaBoxError
    def oversized(*args, **kwargs):
        raise LumaBoxError("SIZE_LIMIT", diagnostics={"limit_bytes": 1048576, "actual_bytes": 2097152})
    monkeypatch.setattr(runner, "download_resource", oversized)
    result = await runner.run_download("https://www.douyin.com/video/7686430330920075249", tmp_path, True, video_limit_mib=1)
    assert result["error"] == "SIZE_LIMIT"
    assert result["diagnostics"]["actual_bytes"] == 2097152
    assert result["diagnostics"]["limit_bytes"] == 1048576


@pytest.mark.parametrize("limit", [0, -1, 8193, True, 1.5])
async def test_invalid_video_limit_is_rejected_before_browser(tmp_path, limit):
    result = await runner.run_download("https://www.douyin.com/video/7686430330920075249", tmp_path, True, video_limit_mib=limit)
    assert result["error"] == "INVALID_INPUT"
