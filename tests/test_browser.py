from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from lumabox.browser import BrowserSession, NavigationGuard
from lumabox.errors import LumaBoxError
from lumabox.extract import DOM_SNAPSHOT, extract_work, detect_access
from lumabox.urls import parse_input
import lumabox.browser as browser_module


def test_every_document_hop_is_checked():
    guard = NavigationGuard()
    for index in range(6):
        guard.check(f"https://v.douyin.com/hop{index}", str(index - 1) if index else None)
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        guard.check("https://v.douyin.com/hop6", "5")
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        NavigationGuard().check("https://evil.test/path", None)


@pytest.mark.parametrize("text,media,code", [("请完成下方验证", True, "CHALLENGE_REQUIRED"), ("请先登录后观看", False, "AUTH_REQUIRED"), ("登录后免费畅享高清视频", True, None)])
def test_access_states(text, media, code):
    if code:
        with pytest.raises(LumaBoxError, match=code):
            detect_access(text, media)
    else:
        detect_access(text, media)


async def test_real_dom_ignores_recommendations_and_backgrounds():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(Path("tests/fixtures/gallery.html").read_text(encoding="utf-8"))
            snapshot = await page.evaluate(DOM_SNAPSHOT, {"work_id": "7695689642767235187", "kind": "gallery"})
            work = extract_work(snapshot, "7695689642767235187")
            assert [r.url for r in work.resources] == ["https://p3.douyinpic.com/a.jpeg", "https://p3.douyinpic.com/b.jpeg"]
        finally:
            await browser.close()


@pytest.mark.parametrize("status,code", [(401, "AUTH_REQUIRED"), (403, "BLOCKED"), (429, "RATE_LIMITED")])
async def test_session_stops_on_document_access_failure(monkeypatch, status, code):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    async with BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=status, body="access denied"))
        with pytest.raises(LumaBoxError, match=code):
            await session.load(parse_input("https://www.douyin.com/note/7695689642767235187"))


async def test_session_extracts_fixture_and_closes_context(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
    async with BrowserSession(headless=True) as session:
        async def serve(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.abort()
        await session.context.route("**/*", serve)
        work = await session.load(parse_input("https://www.douyin.com/note/7695689642767235187"))
        assert len(work.resources) == 2
        assert session.http_status == 200
    assert not session.browser.is_connected()


async def test_session_waits_for_blob_to_become_normal_url(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    html = '''<div data-e2e="feed-active-video" data-e2e-vid="7686430330920075249" style="width:300px;height:200px"><div class="basePlayerContainer"><video src="blob:fixture"></video></div></div>
    <script>setTimeout(()=>{document.querySelector('video').src='https://v1.douyinvod.com/a.mp4'},300)</script>'''
    async with BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html) if route.request.resource_type == "document" else route.abort())
        work = await session.load(parse_input("https://www.douyin.com/video/7686430330920075249"))
        assert work.resources[0].url == "https://v1.douyinvod.com/a.mp4"


async def test_absent_resources_have_bounded_wait(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", 0.1)
    async with BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=200, body="<body>loading</body>"))
        with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
            await session.load(parse_input("https://www.douyin.com/video/7686430330920075249"))


async def test_cdp_checks_real_redirect_before_unallowed_navigation(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    original = BrowserSession._document
    async def redirect_first(self, event):
        if event["request"]["url"].startswith("https://www.douyin.com/video/"):
            self.guard.check(event["request"]["url"], None, event["requestId"])
            await self.cdp.send("Fetch.fulfillRequest", {"requestId": event["requestId"], "responseCode": 302,
                "responseHeaders": [{"name": "Location", "value": "https://evil.test/private"}, {"name": "Content-Length", "value": "0"}]})
        else:
            await original(self, event)
    monkeypatch.setattr(BrowserSession, "_document", redirect_first)
    async with BrowserSession(headless=True) as session:
        with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
            await session.load(parse_input("https://www.douyin.com/video/7686430330920075249"))


async def test_short_link_cannot_switch_work_while_waiting_for_media(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    html = '''<div data-e2e="feed-active-video" data-e2e-vid="7686430330920075249" style="width:300px;height:200px"><div class="basePlayerContainer"><video src="blob:fixture"></video></div></div>
    <script>
    history.replaceState(null,'','https://v.douyin.com/video/7686430330920075249');
    setTimeout(()=>{
      history.replaceState(null,'','https://v.douyin.com/video/7663050581984430449');
      document.querySelector('[data-e2e-vid]').setAttribute('data-e2e-vid','7663050581984430449');
      document.querySelector('video').src='https://v1.douyinvod.com/other.mp4';
    },600);
    </script>'''
    async with BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html) if route.request.resource_type == "document" else route.abort())
        with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
            await session.load(parse_input("https://v.douyin.com/fixture/"))


@pytest.mark.parametrize("defect", ["wrong_owner", "no_owner", "rotated", "clone", "unproven_layout"])
async def test_gallery_dom_rejects_wrong_identity_or_unproven_positions(defect):
    html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
    if defect == "no_owner":
        html = html.replace('data-e2e-vid="7695689642767235187"', '')
    elif defect == "wrong_owner":
        html = html.replace('data-e2e-vid="7695689642767235187"', 'data-e2e-vid="7686430330920075249"')
    if defect == "rotated":
        # The viewport is at the last physical slot, but the counter says first:
        # a wrapped/rotated carousel is not a proven complete linear sequence.
        html += '<style>.track{transform:translateX(-400px)}</style>'
    if defect == "clone":
        html = html.replace('class="dySwiperSlide"', 'class="dySwiperSlide swiper-slide-duplicate"', 1)
    if defect == "unproven_layout":
        html += '<style>.track{display:block}</style>'
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            snapshot = await page.evaluate(DOM_SNAPSHOT, {"work_id": "7695689642767235187", "kind": "gallery"})
            with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
                extract_work(snapshot, "7695689642767235187")
        finally:
            await browser.close()


async def test_gallery_counter_anchors_positions_at_second_image():
    html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
    html = html.replace('<span>1</span>', '<span>2</span>') + '<style>.track{transform:translateX(-400px)}</style>'
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            snapshot = await page.evaluate(DOM_SNAPSHOT, {"work_id": "7695689642767235187", "kind": "gallery"})
            work = extract_work(snapshot, "7695689642767235187")
            assert [r.url for r in work.resources] == ["https://p3.douyinpic.com/a.jpeg", "https://p3.douyinpic.com/b.jpeg"]
        finally:
            await browser.close()
