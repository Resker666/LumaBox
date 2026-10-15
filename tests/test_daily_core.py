"""Daily title and progress contracts, without external media requests."""
import importlib
import json
import asyncio
import time
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from lumabox.extract import DOM_SNAPSHOT, extract_work
from lumabox.models import MediaResource, Work
from lumabox.video_response import extract_detail_video
import lumabox.runner as runner
from test_transfer import server
from test_video_responses import blob_page, detail_payload, load_fixture

VIDEO_ID = "7686430330920075249"
GALLERY_ID = "7695689642767235187"


def test_title_normalization_is_bounded_and_removes_controls():
    metadata = importlib.import_module("lumabox.metadata")
    assert metadata.normalize_title("  光\x00匣\u200b\n  日常\t记录  ") == "光匣 日常 记录"
    assert metadata.normalize_title("x" * 350) == "x" * 300
    assert metadata.normalize_title(" \x00\u200b ") is None
    assert metadata.normalize_title({"desc": "wrong shape"}) is None


def test_legacy_work_and_confirmed_snapshot_have_optional_title():
    assert Work(VIDEO_ID, "video", (MediaResource("https://v1.douyinvod.com/a.mp4", 1),)).title is None
    work = extract_work({"work_id": VIDEO_ID, "kind": "video", "scope_confirmed": True,
        "count": 1, "title": "  标题\n说明\x00 ",
        "resources": [{"order": 1, "role": "video", "url": "https://v1.douyinvod.com/a.mp4"}]}, VIDEO_ID)
    assert work.title == "标题 说明"


@pytest.mark.parametrize("desc,want", [(" 正常\n标题 ", "正常 标题"), (None, None), ({"bad": 1}, None)])
def test_detail_title_belongs_to_same_confirmed_work(desc, want):
    payload = {"status_code": 0, "aweme_detail": {"aweme_id": VIDEO_ID, "aweme_type": 0,
        "desc": desc, "images": None, "video": {"play_addr_h264": {
            "url_list": ["https://v1.douyinvod.com/a.mp4"]}}}}
    assert extract_detail_video(payload, VIDEO_ID).title == want
    assert extract_detail_video(payload, GALLERY_ID) is None


async def test_browser_prefers_bound_caption_when_detail_supplies_media(monkeypatch):
    payload = detail_payload()
    payload["aweme_detail"]["desc"] = "detail fallback"
    html = blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"]).replace(
        '<div class="basePlayerContainer">', '<p data-e2e="video-desc">  页面标题 </p><div class="basePlayerContainer">')
    work, _ = await load_fixture(monkeypatch, html, [("/aweme/detail/", payload, 200)])
    assert work.title == "页面标题"


@pytest.mark.parametrize("kind", ["video", "gallery"])
async def test_real_dom_caption_is_scoped_to_confirmed_work(kind):
    if kind == "gallery":
        html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
        html = html.replace('<div class="basePlayerContainer">',
            '<p data-e2e="note-desc"> 图集标题 </p><div class="basePlayerContainer">', 1)
        target, want = GALLERY_ID, "图集标题"
    else:
        target, want = VIDEO_ID, "作品标题"
        html = f'''<main data-e2e="video-detail"><div class="video-detail-container video_{VIDEO_ID}" data-e2e="player-container">
          <div class="basePlayerContainer"><video src="https://v1.douyinvod.com/a.mp4"></video></div></div>
          <aside><p data-e2e="video-desc"> 作品标题 </p></aside></main>'''
    html += '<title>unrelated page title</title><p data-e2e="video-desc">recommendation</p>'
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            snapshot = await page.evaluate(DOM_SNAPSHOT, {"work_id": target, "kind": kind})
            assert extract_work(snapshot, target).title == want
        finally:
            await browser.close()


def test_progress_snapshot_throttles_atomic_writes_and_excludes_private_fields(tmp_path):
    progress = importlib.import_module("lumabox.progress")
    now = [0.0]
    target = tmp_path / ".progress.json"
    reporter = progress.ProgressFile(target, clock=lambda: now[0])
    assert reporter.emit(stage="media_transfer", work_id=VIDEO_ID, kind="video", title=" 标题 ",
        bytes_received=3, bytes_total=10, expected_count=1, resource_index=1, url="secret", cookie="secret")
    first = json.loads(target.read_bytes())
    assert first == {"stage": "media_transfer", "work_id": VIDEO_ID, "kind": "video", "title": "标题",
        "bytes_received": 3, "bytes_total": 10, "completed_count": 0, "expected_count": 1, "resource_index": 1}
    assert not reporter.emit(bytes_received=6)
    assert json.loads(target.read_bytes())["bytes_received"] == 3
    now[0] = .5
    assert reporter.emit(bytes_received=8)
    assert json.loads(target.read_bytes())["bytes_received"] == 8
    assert reporter.emit(stage="media_verification", bytes_received=10)
    assert progress.read_progress(target)["stage"] == "media_verification"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("field,value", [("bytes_received", True), ("bytes_total", float("inf")),
    ("expected_count", -1), ("resource_index", 0), ("completed_count", 101), ("bytes_received", 2**53)])
def test_progress_rejects_invalid_numeric_metadata(tmp_path, field, value):
    progress = importlib.import_module("lumabox.progress")
    target = tmp_path / ".progress.json"
    target.write_text(json.dumps({"stage": "media_transfer", field: value}), encoding="utf-8")
    assert progress.read_progress(target) is None


def test_progress_rejects_oversized_or_incomplete_snapshot(tmp_path):
    progress = importlib.import_module("lumabox.progress")
    target = tmp_path / ".progress.json"
    target.write_bytes(b"{" + b" " * 20000)
    assert progress.read_progress(target) is None
    target.write_bytes(b'{"stage":')
    assert progress.read_progress(target) is None


@pytest.mark.parametrize("snapshot", [{"stage": []}, {"stage": {}}, {"stage": "media_transfer", "kind": []}])
def test_untrusted_progress_shapes_never_raise(snapshot):
    progress = importlib.import_module("lumabox.progress")
    assert progress.sanitize_snapshot(snapshot) is None


def test_atomic_snapshot_write_failure_preserves_previous_metadata(tmp_path, monkeypatch):
    progress = importlib.import_module("lumabox.progress")
    target = tmp_path / ".progress.json"
    reporter = progress.ProgressFile(target)
    assert reporter.emit(stage="browser_start")
    original = target.read_bytes()
    def denied(*args):
        raise PermissionError("busy destination")
    monkeypatch.setattr(progress.os, "replace", denied)
    assert not reporter.emit(stage="browser_load")
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_cli_progress_destination_must_be_private_output_snapshot(tmp_path):
    progress = importlib.import_module("lumabox.progress")
    assert progress.validate_progress_path(tmp_path, tmp_path / ".progress.json") == tmp_path / ".progress.json"
    for path in [tmp_path / "report.json", tmp_path / "nested" / ".progress.json", tmp_path.parent / ".progress.json"]:
        with pytest.raises(ValueError):
            progress.validate_progress_path(tmp_path, path)


@pytest.mark.parametrize("kind", ["video", "gallery"])
async def test_runner_reports_actual_aggregate_transfer_and_verified_resource_counts(tmp_path, monkeypatch, media_fixtures, server, kind):
    target = VIDEO_ID if kind == "video" else GALLERY_ID
    payload = media_fixtures[0 if kind == "video" else 1].read_bytes()
    def slow(writer):
        chunk = len(payload) // 3
        for index in range(3):
            writer.write(payload[index * chunk:(index + 1) * chunk] if index < 2 else payload[index * chunk:])
            writer.flush()
            if index < 2:
                time.sleep(.6)
    server[0]["/start"] = (200, {"Content-Type": "video/mp4" if kind == "video" else "image/jpeg",
        "Content-Length": str(len(payload))}, slow)
    resources = tuple(MediaResource("https://p3.douyinpic.com/start", index) for index in range(1, 2 if kind == "video" else 3))
    class Session:
        http_status = 200
        def __init__(self, headless):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def load(self, link):
            return Work(target, kind, resources, "  每日\n标题 ")
        async def media_headers(self, url):
            return {}
    monkeypatch.setattr(runner, "BrowserSession", Session)
    snapshots = []
    task = asyncio.create_task(runner.run_download(f"https://www.douyin.com/{'video' if kind == 'video' else 'note'}/{target}",
        tmp_path, True, progress_file=tmp_path / ".progress.json"))
    while not task.done():
        try:
            snapshots.append(json.loads((tmp_path / ".progress.json").read_bytes()))
        except FileNotFoundError:
            pass
        await asyncio.sleep(.01)
    result = await task
    assert result["status"] == "success"
    assert result["title"] == "每日 标题"
    assert json.loads(Path(result["report_path"]).read_bytes())["title"] == "每日 标题"
    final = json.loads((tmp_path / ".progress.json").read_bytes())
    assert final["stage"] == "publication"
    assert final["bytes_received"] == len(payload) * len(resources)
    assert final["completed_count"] == final["expected_count"] == len(resources)
    assert final["resource_index"] is None
    assert any(0 < snapshot["bytes_received"] < final["bytes_received"] for snapshot in snapshots)
    if kind == "gallery":
        assert all(snapshot["bytes_total"] is None for snapshot in snapshots)
    else:
        assert final["bytes_total"] == len(payload)
    assert all(snapshot["completed_count"] <= snapshot["expected_count"] for snapshot in snapshots)


def test_cli_rejects_progress_outside_output_before_touching_files(tmp_path):
    output = tmp_path / "Output"
    outside = tmp_path / ".progress.json"
    process = subprocess.run([sys.executable, "-m", "lumabox", "bad input", "--authorized",
        "--output", str(output), "--progress-file", str(outside)], capture_output=True, text=True)
    assert process.returncode == 2
    assert "output directory" in process.stderr
    assert not outside.exists() and not output.exists()


def test_cli_private_progress_option_keeps_one_final_json_stdout(tmp_path):
    process = subprocess.run([sys.executable, "-m", "lumabox", "bad input", "--authorized",
        "--output", str(tmp_path), "--progress-file", str(tmp_path / ".progress.json")], capture_output=True, text=True)
    assert process.returncode == 1
    assert len(process.stdout.splitlines()) == 1
    result = json.loads(process.stdout)
    assert result["error"] == "INVALID_INPUT" and result["title"] is None
