"""Mixed gallery previews and actionable extraction errors in real browsers."""
import json

import pytest
from playwright.sync_api import expect

from test_daily_ui import browser, daily_server, wait_finished


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_live_note_shows_picture_and_motion_and_downloads_both(daily_server, browser, media_fixtures):
    url, worker = daily_server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    child = worker.script.read_text(encoding="utf-8")
    child = child.replace("(dest/'report.json').write_text", f'''
motion=Path({json.dumps(str(media_fixtures[0]))}).read_bytes()
(dest/'01-motion.mp4').write_bytes(motion)
result['files'].append({{'name':'01-motion.mp4','bytes':len(motion),'sha256':hashlib.sha256(motion).hexdigest(),
    'width':64,'height':48,'duration':.4}})
result['bytes']+=len(motion)
(dest/'report.json').write_text''')
    worker.script.write_text(child, encoding="utf-8")
    job = worker.store.create("https://v.douyin.com/test/")
    finished = wait_finished(worker.store, job["id"])
    assert len(finished["files"]) == 3
    context = browser.new_context(viewport={"width": 390, "height": 844}, accept_downloads=True)
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_text("1 张图片 · 1 段动态片段", exact=False)).to_be_visible()
        page.get_by_role("button", name="预览图集", exact=True).click()
        expect(page.locator("#preview-image")).to_be_visible()
        page.get_by_role("button", name="下一张", exact=True).click()
        expect(page.locator("#preview-video")).to_be_visible()
        expect(page.locator("#preview-image")).not_to_be_visible()
        page.wait_for_function("() => document.querySelector('#preview-video').readyState >= 1")
        assert page.locator("#preview-video").evaluate("v=>v.duration") > .3
        page.get_by_role("button", name="上一张", exact=True).click()
        expect(page.locator("#preview-image")).to_be_visible()
        assert page.locator("#preview-video").get_attribute("src") is None
        page.get_by_role("button", name="关闭预览", exact=True).click()
        page.get_by_text("另存文件", exact=True).click()
        link = page.get_by_role("link", name="另存动态片段 01", exact=True)
        expect(link).to_be_visible()
        response = page.request.get(url + link.get_attribute("href"))
        assert response.status == 200 and response.body() == media_fixtures[0].read_bytes()
    finally:
        context.close()


def test_media_count_failure_is_explained_in_history(daily_server, browser):
    url, worker = daily_server
    job = worker.store.create("https://v.douyin.com/test/")
    worker.store.fail(job["id"], "UNSUPPORTED_MEDIA", {"diagnostics": {"stage": "media_extraction",
        "reason": "Complete media count could not be confirmed"}})
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_text("已识别作品页面，但无法确认完整页数，暂未保存文件。", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="一键重试", exact=True)).to_be_visible()
    finally:
        context.close()
