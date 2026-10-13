import json
from types import SimpleNamespace

import pytest

import lumabox.browser as browser_module
from lumabox.browser import BrowserSession
from lumabox.runner import run_download


VIDEO_ID = "7686430330920075249"


def media_response(frame, url, mime="video/mp4", status=206, headers=None, resource_type="fetch"):
    # Response metadata is the input to our sanitizer; bodies are never read.
    return SimpleNamespace(url=url, status=status,
        headers={"content-type": mime, "content-length": "4", **(headers or {})},
        request=SimpleNamespace(frame=frame, resource_type=resource_type, is_navigation_request=lambda: False))


def session_for_response():
    session = BrowserSession()
    frame = object()
    session.page = SimpleNamespace(main_frame=frame)
    return session, frame


def test_response_diagnostics_keep_range_evidence_without_signed_urls():
    session, frame = session_for_response()
    session._response(media_response(frame, "https://v1.douyinvod.com/private/segment.m4s?signature=secret",
        headers={"content-range": "bytes 0-3/16", "set-cookie": "session=secret"}))
    assert session.media_responses == [{"host": "v1.douyinvod.com", "resource_type": "fetch", "status": 206,
        "content_type": "video/mp4", "format_hint": "m4s", "ranged": True,
        "total_bytes": 16, "sample_bytes": 4, "count": 1}]
    assert all(value not in json.dumps(session.media_responses) for value in ["secret", "private", "signature", "set-cookie"])


def test_response_diagnostics_ignore_json_and_child_frame_media():
    session, frame = session_for_response()
    session._response(media_response(frame, "https://www.douyin.com/detail?token=secret", mime="application/json"))
    session._response(media_response(object(), "https://v1.douyinvod.com/background.mp4"))
    assert session.media_responses == []
    assert session.media_response_count == 0


def test_response_diagnostics_are_bounded_and_aggregate_repeated_requests():
    session, frame = session_for_response()
    for index in range(25):
        session._response(media_response(frame, f"https://v{index}.douyinvod.com/resource.mp4"))
    session._response(media_response(frame, "https://v0.douyinvod.com/other.mp4?signature=secret"))
    assert len(session.media_responses) == 20
    assert session.media_responses[0]["count"] == 2
    assert session.media_response_count == 26
    assert session.media_response_truncated is True


def test_response_diagnostics_reject_untrusted_header_text():
    session, frame = session_for_response()
    session._response(media_response(frame, "https://v1.douyinvod.com/resource",
        mime="video/private-secret", headers={"content-length": "secret", "content-range": "secret"}))
    assert session.media_responses[0]["content_type"] == "video/other"
    assert session.media_responses[0]["sample_bytes"] is None
    assert session.media_responses[0]["total_bytes"] is None
    assert "secret" not in json.dumps(session.media_responses)


async def test_blob_failure_report_includes_real_browser_media_response_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(browser_module, "RESOURCE_WAIT", 2)
    original_enter = BrowserSession.__aenter__
    html = f'''<div data-e2e="feed-active-video" data-e2e-vid="{VIDEO_ID}">
      <div class="basePlayerContainer"><video src="blob:fixture"></video></div>
    </div><script>
      fetch('https://v1.douyinvod.com/private/segment.m4s?signature=secret');
      fetch('https://v1.douyinvod.com/private/sound.m4a?signature=secret');
      fetch('https://v1.douyinvod.com/private/list.m3u8?signature=secret');
    </script>'''

    async def fixture_enter(session):
        await original_enter(session)
        async def serve(route):
            url = route.request.url
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            elif "segment.m4s" in url:
                await route.fulfill(status=206, body=b"1234", headers={"content-type": "video/mp4",
                    "content-range": "bytes 0-3/16", "access-control-allow-origin": "*", "set-cookie": "secret=secret"})
            elif "sound.m4a" in url:
                await route.fulfill(status=200, body=b"12345678", headers={"content-type": "audio/mp4", "access-control-allow-origin": "*"})
            elif "list.m3u8" in url:
                await route.fulfill(status=200, body="#EXTM3U", headers={"content-type": "application/vnd.apple.mpegurl", "access-control-allow-origin": "*"})
            else:
                await route.abort()
        await session.context.route("**/*", serve)
        return session

    monkeypatch.setattr(BrowserSession, "__aenter__", fixture_enter)
    result = await run_download(f"https://www.douyin.com/video/{VIDEO_ID}", tmp_path, True)
    assert result["error"] == "UNSUPPORTED_MEDIA"
    assert result["diagnostics"]["media_response_count"] == 3
    assert sorted(r["format_hint"] for r in result["diagnostics"]["media_responses"]) == ["m3u8", "m4a", "m4s"]
    video = next(r for r in result["diagnostics"]["media_responses"] if r["format_hint"] == "m4s")
    assert video["status"] == 206
    assert video["ranged"] is True
    assert video["total_bytes"] == 16
    assert all(value not in json.dumps(result) for value in ["private", "secret", "signature=", "#EXTM3U"])
