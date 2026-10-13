"""The player may stay blob while its normal detail response supplies a full MP4."""
import json

import pytest

import lumabox.browser as browser_module
from lumabox.browser import BrowserSession
from lumabox.errors import LumaBoxError
from lumabox.urls import parse_input, validate_media_url

VIDEO_ID = "7686430330920075249"
OTHER_ID = "7663050581984430449"
MEDIA_URL = "https://v5-hl-mly-ov.zjcdn.com/target-file?signature=secret"


def detail_payload(work_id=VIDEO_ID, url=MEDIA_URL):
    # Reduced, non-sensitive shape observed in a normal detail response.
    return {"status_code": 0, "aweme_detail": {"aweme_id": work_id, "aweme_type": 0,
        "images": None, "video": {"format": "mp4", "duration": 124600,
            "play_addr_h264": {"url_list": [url], "width": 1920, "height": 1080, "data_size": 3316797},
            "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]}}}}


async def load_fixture(monkeypatch, html, replies, target=VIDEO_ID, short=False):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", 1.5)
    async with BrowserSession(headless=True) as session:
        async def serve(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                for path, body, status, *extra_headers in replies:
                    if path in route.request.url:
                        await route.fulfill(status=status, content_type="application/json", body=json.dumps(body), headers={"Access-Control-Allow-Origin": "*", **(extra_headers[0] if extra_headers else {})})
                        return
                await route.abort()
        await session.context.route("**/*", serve)
        link = parse_input("https://v.douyin.com/fixture/" if short else f"https://www.douyin.com/video/{target}")
        return await session.load(link), session.diagnostics.copy()


def blob_page(requests, owner=VIDEO_ID, prefix=""):
    return '<meta charset="utf-8">' + prefix + f'''<div data-e2e="feed-active-video" data-e2e-vid="{owner}" style="width:300px;height:200px">
      <div class="basePlayerContainer"><video src="blob:fixture"></video></div></div>
      <script>{''.join(f"fetch('{url}');" for url in requests)}</script>'''


async def test_normal_target_detail_provides_full_video_while_dom_stays_blob(monkeypatch):
    html = blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}&a_bogus=secret"])
    work, diagnostics = await load_fixture(monkeypatch, html, [("/aweme/detail/", detail_payload(), 200)])
    assert work.work_id == VIDEO_ID
    assert work.kind == "video"
    assert [resource.url for resource in work.resources] == [MEDIA_URL]
    assert diagnostics["resource_source"] == "normal_detail_response"
    assert diagnostics["detail_response_count"] == 1
    assert diagnostics["detail_metadata_parsed"] is True
    assert "signature" not in json.dumps(diagnostics)
    assert "secret" not in json.dumps(diagnostics)


@pytest.mark.parametrize("defect", ["wrong_query", "wrong_payload", "related_endpoint", "child_frame", "wrong_dom", "failed_status", "gallery_payload"])
async def test_normal_response_cannot_supply_unbound_or_recommended_video(monkeypatch, defect):
    query_id = OTHER_ID if defect == "wrong_query" else VIDEO_ID
    path = "/aweme/v1/web/aweme/related/" if defect == "related_endpoint" else "/aweme/v1/web/aweme/detail/"
    payload = detail_payload(OTHER_ID if defect == "wrong_payload" else VIDEO_ID)
    if defect == "failed_status":
        payload["status_code"] = 1
    if defect == "gallery_payload":
        payload["aweme_detail"]["images"] = [{"url_list": ["https://p3.douyinpic.com/one.jpeg"]}]
    request = f"{path}?aweme_id={query_id}"
    if defect == "child_frame":
        from urllib.parse import quote
        child = f"<script>fetch('{request}')</script>"
        html = blob_page([], prefix=f'<iframe src="data:text/html,{quote(child)}"></iframe>')
    else:
        html = blob_page([request], owner=OTHER_ID if defect == "wrong_dom" else VIDEO_ID)
    with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
        await load_fixture(monkeypatch, html, [(path, payload, 200)])


async def test_normal_response_still_rejects_unverified_media_host(monkeypatch):
    html = blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"])
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        await load_fixture(monkeypatch, html, [("/aweme/detail/", detail_payload(url="https://evil.test/file.mp4"), 200)])


async def test_normal_response_does_not_override_explicit_access_challenge(monkeypatch):
    html = blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"], prefix="<p>请完成下方验证</p>")
    with pytest.raises(LumaBoxError, match="CHALLENGE_REQUIRED"):
        await load_fixture(monkeypatch, html, [("/aweme/detail/", detail_payload(), 200)])


@pytest.mark.parametrize("status,code", [(401,"AUTH_REQUIRED"),(403,"BLOCKED"),(429,"RATE_LIMITED")])
async def test_target_detail_access_failure_stops_without_fallback(monkeypatch,status,code):
    html = blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"])
    with pytest.raises(LumaBoxError,match=code):
        await load_fixture(monkeypatch,html,[("/aweme/detail/",detail_payload(),status)])


@pytest.mark.parametrize("defect", ["invalid_json_shape","large_body","http_media","credentials","fragment_media"])
async def test_normal_response_rejects_invalid_or_oversized_payload(monkeypatch,defect):
    payload = detail_payload()
    if defect == "invalid_json_shape":
        payload = "not a detail object"
    elif defect == "large_body":
        payload['padding'] = 'a' * (2*1024*1024+1)
    else:
        payload['aweme_detail']['video']['play_addr_h264']['url_list'] = [{'http_media':'http://v5-hl-mly-ov.zjcdn.com/file','credentials':'https://secret@v5-hl-mly-ov.zjcdn.com/file','fragment_media':'https://v5-hl-mly-ov.zjcdn.com/file#segment'}[defect]]
    html = blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"])
    with pytest.raises(LumaBoxError,match="UNSUPPORTED_MEDIA|UNSAFE_TARGET"):
        await load_fixture(monkeypatch,html,[("/aweme/detail/",payload,200)])


async def test_deeply_nested_json_does_not_escape_the_response_reader():
    from types import SimpleNamespace
    session = BrowserSession()
    async def send(method,args):
        return {'body':'['*1200 + '0' + ']'*1200,'base64Encoded':False}
    session.cdp = SimpleNamespace(send=send)
    await session._read_detail_response('fixture-request',VIDEO_ID)
    assert session.detail_work is None


async def test_small_wire_header_cannot_cause_large_decoded_body_copy(monkeypatch):
    import gzip
    from playwright.async_api import Response
    payload = detail_payload()
    payload['padding'] = 'a' * (3*1024*1024)
    raw = json.dumps(payload).encode()
    original_body = Response.body
    copies = []
    async def measured_body(response):
        body = await original_body(response)
        if '/aweme/detail/' in response.url:
            copies.append(len(body))
        return body
    monkeypatch.setattr(Response,'body',measured_body)
    html = blob_page([f'/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}'])
    # Fulfill supplies the already decoded body with wire-sized gzip headers.
    # The wire header is not a bound on the body copied through browser IPC.
    with pytest.raises(LumaBoxError,match='UNSUPPORTED_MEDIA'):
        await load_fixture(monkeypatch,html,[('/aweme/detail/',payload,200,{'Content-Encoding':'gzip','Content-Length':str(len(gzip.compress(raw)))})])
    assert copies == []


async def test_short_link_binds_normal_response_to_first_confirmed_work(monkeypatch):
    prefix = f"<script>history.replaceState(null,'','/video/{VIDEO_ID}')</script>"
    html = blob_page([f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"], prefix=prefix)
    work, _ = await load_fixture(monkeypatch, html, [("/aweme/detail/", detail_payload(), 200)], short=True)
    assert [resource.url for resource in work.resources] == [MEDIA_URL]


async def test_normal_response_waits_through_real_navigation_context_change(monkeypatch):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", 3)
    original = browser_module.DOM_SNAPSHOT
    # The first real document reloads while our DOM read is pending.
    monkeypatch.setattr(browser_module, "DOM_SNAPSHOT", "async (arg) => { if(document.getElementById('loading')) await new Promise(r=>setTimeout(r,1000)); return (" + original + ")(arg); }")
    document_count = 0
    async with BrowserSession(headless=True) as session:
        async def serve(route):
            nonlocal document_count
            if route.request.resource_type == "document":
                document_count += 1
                html = '<p id="loading">loading</p><script>setTimeout(()=>location.reload(),150)</script>' if document_count == 1 else blob_page([f"/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}"])
                await route.fulfill(status=200, content_type="text/html", body=html)
            elif "/aweme/detail/" in route.request.url:
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(detail_payload()))
            else:
                await route.abort()
        await session.context.route("**/*", serve)
        work = await session.load(parse_input(f"https://www.douyin.com/video/{VIDEO_ID}"))
        assert [resource.url for resource in work.resources] == [MEDIA_URL]


@pytest.mark.parametrize("host", ["v5-hl-mly-ov.zjcdn.com", "v5-dy-ov-experiment.zjcdn.com"])
def test_only_observed_zjcdn_hosts_can_supply_media(host):
    assert validate_media_url(f"https://{host}/file") == f"https://{host}/file"


@pytest.mark.parametrize("host", ["zjcdn.com", "other.zjcdn.com", "v5-hl-mly-ov.zjcdn.com.evil.test"])
def test_zjcdn_approval_does_not_extend_to_other_hosts(host):
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        validate_media_url(f"https://{host}/file")
