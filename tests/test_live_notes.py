"""Counterless single Live Photo, paired files, and safe failure diagnostics."""
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import lumabox.runner as runner
from lumabox.errors import LumaBoxError
from lumabox.extract import DOM_SNAPSHOT, extract_work
from lumabox.models import MediaResource, Work
from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from lumabox.web.worker import JobWorker

ID = "7696824302280965050"
URL = f"https://www.douyin.com/note/{ID}"


@pytest.fixture
async def note_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        await page.set_content(Path("tests/fixtures/single-live-note.html").read_text(encoding="utf-8"))
        try:
            yield page
        finally:
            await browser.close()


async def test_counterless_single_note_keeps_still_and_scoped_motion(note_page):
    # Rejecting all counterless notes caused the production failure; selecting
    # the player's first video would silently save background music instead.
    snapshot = await note_page.evaluate(DOM_SNAPSHOT, {"work_id": ID, "kind": "gallery"})
    work = extract_work(snapshot, ID)
    assert work.kind == "gallery" and len(work.resources) == 1
    assert work.resources[0].url == "https://p3.douyinpic.com/still.jpeg"
    assert work.resources[0].motion_url in ("https://v1.douyinvod.com/motion.mp4", "https://v2.douyinvod.com/motion.mp4")


@pytest.mark.parametrize("defect", ["no_single_marker", "extra_slide", "wrong_id", "shifted", "clone", "two_motion_players", "missing_motion_player", "blob_motion", "untrusted_motion"])
async def test_single_note_needs_completeness_identity_and_valid_motion(note_page, defect):
    mutations = {
        "no_single_marker": "document.querySelector('xg-progress').classList.remove('one-live-hide')",
        "extra_slide": "document.querySelector('.track').append(document.querySelector('.dySwiperSlide').cloneNode(true))",
        "wrong_id": "document.querySelector('[data-e2e-vid]').setAttribute('data-e2e-vid','7686430330920075249')",
        "shifted": "document.querySelector('.track').style.transform='translateX(-400px)'",
        "clone": "document.querySelector('.dySwiperSlide').classList.add('swiper-slide-duplicate')",
        "two_motion_players": "document.querySelector('.dySwiperSlide').append(document.createElement('video'))",
        "missing_motion_player": "document.querySelector('.dySwiperSlide video').remove()",
        "blob_motion": "document.querySelector('.dySwiperSlide video').src='blob:https://www.douyin.com/private'",
        "untrusted_motion": "document.querySelector('.dySwiperSlide video').src='https://unverified.example/motion.mp4'",
    }
    await note_page.evaluate(mutations[defect])
    snapshot = await note_page.evaluate(DOM_SNAPSHOT, {"work_id": ID, "kind": "gallery"})
    with pytest.raises(LumaBoxError):
        extract_work(snapshot, ID)


@pytest.fixture
def live_pipeline(monkeypatch, media_fixtures):
    class Session:
        http_status = 200
        def __init__(self, headless): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def load(self, link):
            return Work(ID, "gallery", (MediaResource("https://p3.douyinpic.com/still.jpeg", 1,
                motion_url="https://v1.douyinvod.com/motion.mp4"),), "单页动态照片")
        async def media_headers(self, url): return {}
    def save(resource, path, headers, limit, cancel=None, **kwargs):
        source = media_fixtures[0 if path.suffix == ".mp4" else 1]
        shutil.copyfile(source, path)
        return path.stat().st_size
    monkeypatch.setattr(runner, "BrowserSession", Session)
    monkeypatch.setattr(runner, "download_resource", save)
    return save


async def test_single_note_publishes_verified_paired_files_and_zip(tmp_path, live_pipeline):
    result = await runner.run_download(URL, tmp_path)
    assert result["status"] == "success", result
    assert result["actual_count"] == result["expected_count"] == 1
    assert [f["name"] for f in result["files"]] == ["01.jpg", "01-motion.mp4"]
    assert result["files"][0]["duration"] is None
    assert result["files"][1]["duration"] > 0
    with zipfile.ZipFile(Path(result["output_directory"]) / f"{ID}-images.zip") as archive:
        assert archive.namelist() == ["01.jpg", "01-motion.mp4", "report.json"]
        assert archive.testzip() is None


async def test_failed_motion_never_publishes_still_only_success(tmp_path, live_pipeline, monkeypatch):
    attempted = []
    def save(resource, path, *args, **kwargs):
        attempted.append(path.name)
        if path.suffix == ".mp4":
            raise LumaBoxError("DOWNLOAD_FAILED")
        return live_pipeline(resource, path, *args, **kwargs)
    monkeypatch.setattr(runner, "download_resource", save)
    result = await runner.run_download(URL, tmp_path)
    assert attempted == ["01.jpg", "01-motion.mp4"]
    assert result["status"] == "failed" and result["error"] == "DOWNLOAD_FAILED"
    assert result["files"] == [] and result["actual_count"] == 0
    assert not list(tmp_path.glob(".partial-*")) and not list(tmp_path.glob("*/01.jpg"))


async def test_web_publication_accepts_pair_but_rejects_missing_still(tmp_path, live_pipeline):
    config = WebConfig(tmp_path / "data")
    store = JobStore(config)
    job = store.create(URL)
    directory = config.output_dir / job["id"]
    result = await runner.run_download(URL, directory)
    assert result["status"] == "success", result
    files = JobWorker(config, store)._verify(job, directory, result)
    assert [f["mime"] for f in files] == ["image/jpeg", "video/mp4", "application/zip"]
    with zipfile.ZipFile(Path(result["output_directory"]) / f"{ID}-images.zip") as archive:
        report = json.loads(archive.read("report.json"))
        assert report["actual_count"] == 1 and len(report["files"]) == 2
        assert "output_directory" not in report
    result["files"] = result["files"][1:]
    Path(result["report_path"]).write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(Exception, match="VERIFY_FAILED"):
        JobWorker(config, store)._verify(job, directory, result)


def test_history_keeps_known_media_failure_without_raw_details(tmp_path):
    store = JobStore(WebConfig(tmp_path / "data"))
    job = store.create(URL)
    store.fail(job["id"], "UNSUPPORTED_MEDIA", {"diagnostics": {"stage": "media_extraction",
        "reason": "Complete media count could not be confirmed", "url": "https://private/?secret=1"}})
    assert store.get(job["id"])["error_detail"] == {
        "stage": "media_extraction", "media_reason": "MEDIA_COUNT_UNCONFIRMED"}
    assert "secret" not in json.dumps(store.get(job["id"]))


async def test_short_link_failure_retains_confirmed_work_identity_and_title(tmp_path, monkeypatch):
    import lumabox.browser as browser_module
    from lumabox.browser import BrowserSession
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", .3)
    html = Path("tests/fixtures/single-live-note.html").read_text(encoding="utf-8").replace("one-live-hide", "")
    html += f"<script>history.replaceState(null,'','https://v.douyin.com/note/{ID}')</script>"
    original = BrowserSession.__aenter__
    async def enter(session):
        await original(session)
        await session.context.route("**/*", lambda route: route.fulfill(status=200,
            content_type="text/html; charset=utf-8", body=html) if route.request.resource_type == "document" else route.abort())
        return session
    monkeypatch.setattr(BrowserSession, "__aenter__", enter)
    result = await runner.run_download("https://v.douyin.com/test/", tmp_path, headless=True)
    assert result["status"] == "failed" and result["error"] == "UNSUPPORTED_MEDIA"
    assert result["work_id"] == ID and result["kind"] == "gallery"
    assert result["title"] == "单页动态照片"
    assert result["metadata_parsed"] is False and result["actual_count"] == 0
