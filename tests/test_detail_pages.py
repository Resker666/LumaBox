from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import lumabox.browser as browser_module
from lumabox.browser import BrowserSession
from lumabox.errors import LumaBoxError
from lumabox.extract import DOM_SNAPSHOT, detect_access, extract_work
from lumabox.urls import parse_input


VIDEO_ID = "7686430330920075249"
GALLERY_ID = "7695689642767235187"


def video_detail(source="https://v1.douyinvod.com/a.mp4"):
    # The dedicated video page identifies its player by video_<id>, without
    # a feed-active-video wrapper. The unrelated live player is precreated.
    return f'''<main data-e2e="video-detail">
      <div data-e2e="player-container" class="video-detail-container video_{VIDEO_ID}">
        <div class="basePlayerContainer"><video src="{source}"></video></div>
      </div>
    </main>
    <div hidden><div class="basePlayerContainer"><video></video></div></div>
    <p>请先 登录 后发表评论</p><p>登录后免费畅享高清视频</p>'''


def gallery_detail():
    # On the dedicated note page the active work ID belongs to a sibling
    # sidebar inside the same main, rather than a player ancestor.
    html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
    html = html.replace(
        f'<div data-e2e="feed-active-video" data-e2e-vid="{GALLERY_ID}">',
        '<main data-e2e="note-detail">', 1)
    return html.replace(
        '</div>\n<div class="detailrelatedVideoCard">',
        f'''<aside class="noteSideBarContainer">
          <div data-e2e="feed-active-video" data-e2e-vid="{GALLERY_ID}">播放中</div>
        </aside></main>
        <div class="detailrelatedVideoCard">''', 1)


@pytest.mark.parametrize("text", [
    "请先登录后发表评论", "请先 登录 后发表评论", "请先登录后点赞", "登录后免费畅享高清视频",
])
def test_comment_or_promotion_login_is_not_a_viewing_requirement(text):
    detect_access(text, False)


@pytest.mark.parametrize("text", ["请先 登录 后观看", "登录后才能观看"])
def test_explicit_viewing_login_requirement_still_stops(text):
    with pytest.raises(LumaBoxError, match="AUTH_REQUIRED"):
        detect_access(text, False)


@pytest.mark.parametrize("kind", ["video", "gallery"])
@pytest.mark.parametrize("container_tag", ["main", "div"])
async def test_dedicated_detail_page_binds_media_to_actual_dom_identity(kind, container_tag):
    html = video_detail() if kind == "video" else gallery_detail()
    html = html.replace('<main data-e2e=', f'<{container_tag} data-e2e=').replace('</main>', f'</{container_tag}>')
    target = VIDEO_ID if kind == "video" else GALLERY_ID
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            snapshot = await page.evaluate(DOM_SNAPSHOT, {"work_id": target, "kind": kind})
            work = extract_work(snapshot, target)
            expected = ["https://v1.douyinvod.com/a.mp4"] if kind == "video" else [
                "https://p3.douyinpic.com/a.jpeg", "https://p3.douyinpic.com/b.jpeg"]
            assert [r.url for r in work.resources] == expected
        finally:
            await browser.close()


@pytest.mark.parametrize("defect", [
    "video_wrong_id", "video_no_id", "video_conflicting_ids", "video_two_players", "video_two_detail_regions",
    "gallery_wrong_id", "gallery_no_sidebar", "gallery_outside_main", "gallery_two_active", "gallery_two_players", "gallery_two_detail_regions",
])
async def test_dedicated_detail_rejects_missing_conflicting_or_unrelated_identity(defect):
    kind = "video" if defect.startswith("video") else "gallery"
    target = VIDEO_ID if kind == "video" else GALLERY_ID
    html = video_detail() if kind == "video" else gallery_detail()
    if defect == "video_wrong_id":
        html = html.replace(f"video_{VIDEO_ID}", f"video_{GALLERY_ID}")
    elif defect == "video_no_id":
        html = html.replace(f"video_{VIDEO_ID}", "")
    elif defect == "video_conflicting_ids":
        html = html.replace(f"video_{VIDEO_ID}", f"video_{VIDEO_ID} video_{GALLERY_ID}")
    elif defect == "video_two_players":
        html = html.replace("</main>", video_detail().split("</main>")[0].split(">", 1)[1] + "</main>", 1)
    elif defect == "gallery_wrong_id":
        html = html.replace(f'data-e2e-vid="{GALLERY_ID}"', f'data-e2e-vid="{VIDEO_ID}"')
    elif defect == "gallery_no_sidebar":
        html = html.replace('class="noteSideBarContainer"', 'class="unrelated"')
    elif defect == "gallery_outside_main":
        html = html.replace('<aside class="noteSideBarContainer">', '</main><aside class="noteSideBarContainer">')
        html = html.replace('</aside></main>', '</aside>')
    elif defect == "gallery_two_active":
        html = html.replace('</aside>', f'<div data-e2e="feed-active-video" data-e2e-vid="{VIDEO_ID}">播放中</div></aside>')
    elif defect == "gallery_two_players":
        html = html.replace('<aside class="noteSideBarContainer">',
            '<div data-e2e="player-container" class="note-detail-container"><div class="basePlayerContainer"></div></div><aside class="noteSideBarContainer">')
    if defect.endswith("two_detail_regions"):
        html += f'<div data-e2e="{"video" if kind == "video" else "note"}-detail">另一个详情区域</div>'
    # A matching link elsewhere cannot prove the target player's ownership.
    html += f'<a href="https://www.douyin.com/{"video" if kind == "video" else "note"}/{target}">推荐</a>'
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            snapshot = await page.evaluate(DOM_SNAPSHOT, {"work_id": target, "kind": kind})
            with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
                extract_work(snapshot, target)
        finally:
            await browser.close()


async def test_detail_video_with_login_promotion_waits_for_normal_media(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", 2)
    html = video_detail("blob:fixture") + '''<script>setTimeout(()=>{
      document.querySelector('main video').src='https://v1.douyinvod.com/a.mp4'
    },400)</script>'''
    async with BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=200,
            content_type="text/html; charset=utf-8", body=html) if route.request.resource_type == "document" else route.abort())
        work = await session.load(parse_input(f"https://www.douyin.com/video/{VIDEO_ID}"))
        assert work.resources[0].url == "https://v1.douyinvod.com/a.mp4"


async def test_persistent_blob_reports_resource_limitation_and_safe_diagnostics(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", 0.3)
    html = video_detail("blob:https://www.douyin.com/private-token") + '<p>请先登录后观看</p>'
    async with BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=200,
            content_type="text/html; charset=utf-8", body=html) if route.request.resource_type == "document" else route.abort())
        with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA") as failure:
            await session.load(parse_input(f"https://www.douyin.com/video/{VIDEO_ID}"))
        assert failure.value.message == "A normal HTTPS media resource is not available"
        assert session.diagnostics["scope_confirmed"] is True
        assert session.diagnostics["media_schemes"] == ["blob"]
        assert "private-token" not in str(session.diagnostics)
