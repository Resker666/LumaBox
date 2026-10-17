"""Counterless static notes need an independent picture-page count."""
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import lumabox.browser as browser_module
import lumabox.runner as runner
from lumabox.browser import BrowserSession
from lumabox.errors import LumaBoxError
from lumabox.extract import DOM_SNAPSHOT, extract_work
from lumabox.web.config import WebConfig
from lumabox.web.store import JobStore
from lumabox.web.worker import JobWorker

ID = "7697415943497351346"
URL = f"https://www.douyin.com/note/{ID}"
IMAGE = "https://p3.douyinpic.com/still.jpeg"
HTML = Path("tests/fixtures/single-static-note.html").read_text(encoding="utf-8")


@pytest.fixture
async def static_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        await page.set_content(HTML)
        try:
            yield page
        finally:
            await browser.close()


async def test_picture_progress_confirms_static_single_note_without_music(static_page):
    snapshot = await static_page.evaluate(DOM_SNAPSHOT, {"work_id": ID, "kind": "gallery"})
    work = extract_work(snapshot, ID)
    assert snapshot["count"] == 1 and work.kind == "gallery"
    assert len(work.resources) == 1 and work.resources[0].url == IMAGE
    assert work.resources[0].motion_url is None


@pytest.mark.parametrize("defect", [
    "first_of_multiple", "no_progress", "outside_progress", "two_controls",
    "not_picture", "nonzero_index", "extra_slide", "unexpected_motion", "wrong_identity", "live_marker_without_motion",
])
async def test_static_single_needs_consistent_picture_progress_and_identity(static_page, defect):
    mutations = {
        "first_of_multiple": "document.querySelector('.progress-list').append(document.querySelector('xg-inner').cloneNode(true))",
        "no_progress": "document.querySelector('xg-progress').remove()",
        "outside_progress": "document.body.append(document.querySelector('xg-progress'))",
        "two_controls": "document.querySelector('xg-controls').append(document.querySelector('xg-progress').cloneNode(true))",
        "not_picture": "document.querySelector('xg-outer').classList.remove('picture')",
        "nonzero_index": "document.querySelector('xg-progress').setAttribute('data-index','1')",
        "extra_slide": "document.querySelector('.track').append(document.querySelector('.dySwiperSlide').cloneNode(true))",
        "unexpected_motion": "document.querySelector('.dySwiperSlide').append(document.createElement('video'))",
        "wrong_identity": "document.querySelector('[data-e2e-vid]').setAttribute('data-e2e-vid','7696824302280965050')",
        "live_marker_without_motion": "document.querySelector('xg-progress').classList.add('one-live-hide')",
    }
    await static_page.evaluate(mutations[defect])
    snapshot = await static_page.evaluate(DOM_SNAPSHOT, {"work_id": ID, "kind": "gallery"})
    with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
        extract_work(snapshot, ID)


async def test_static_note_browser_to_verified_image_and_web_zip(tmp_path, monkeypatch, media_fixtures):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", .4)
    original = BrowserSession.__aenter__
    async def enter(session):
        await original(session)
        await session.context.route("**/*", lambda route: route.fulfill(status=200,
            content_type="text/html; charset=utf-8", body=HTML) if route.request.resource_type == "document" else route.abort())
        return session
    monkeypatch.setattr(BrowserSession, "__aenter__", enter)
    downloaded = []
    def transfer(resource, path, *args, **kwargs):
        downloaded.append(resource.url)
        assert resource.url == IMAGE
        shutil.copyfile(media_fixtures[1], path)
        return path.stat().st_size
    monkeypatch.setattr(runner, "download_resource", transfer)
    config = WebConfig(tmp_path / "data"); store = JobStore(config)
    job = store.create(URL); store.claim()
    result = await runner.run_download(URL, config.output_dir / job["id"], headless=True)
    assert result["status"] == "success", result
    assert result["actual_count"] == result["expected_count"] == 1
    assert downloaded == [IMAGE] and result["checks"]["full_decode"] is True
    assert [entry["name"] for entry in result["files"]] == ["01.jpg"]
    files = JobWorker(config, store)._verify(job, config.output_dir / job["id"], result)
    assert [f["mime"] for f in files] == ["image/jpeg", "application/zip"]
    with zipfile.ZipFile(Path(result["output_directory"]) / f"{ID}-images.zip") as archive:
        assert archive.namelist() == ["01.jpg", "report.json"]
        assert archive.read("01.jpg") == media_fixtures[1].read_bytes()
        assert "output_directory" not in json.loads(archive.read("report.json"))
