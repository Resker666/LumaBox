import json
import zipfile
from pathlib import Path

import pytest
import lumabox.browser as browser_module
from lumabox.cli import main
from test_transfer import server

ID = "7695689642767235187"
VIDEO_ID = "7686430330920075249"


@pytest.mark.parametrize("kind,failed", [("video", False), ("gallery", False), ("gallery", True)])
def test_cli_real_browser_transfer_decode_and_publish(monkeypatch, media_fixtures, server, tmp_path, capsys, kind, failed):
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    if kind == "gallery":
        html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
        url = f"https://www.douyin.com/note/{ID}"
        for filename in ("a.jpeg", "b.jpeg"):
            server[0]["/" + filename] = (403, {}, b"denied") if failed else (200, {"Content-Type": "image/jpeg"}, media_fixtures[1].read_bytes())
    else:
        html = f'<div data-e2e="feed-active-video" data-e2e-vid="{VIDEO_ID}" style="width:300px;height:200px"><div class="basePlayerContainer"><video src="https://v1.douyinvod.com/a.mp4"></video></div></div>'
        url = f"https://www.douyin.com/video/{VIDEO_ID}"
        server[0]["/a.mp4"] = (200, {"Content-Type": "video/mp4"}, media_fixtures[0].read_bytes())
    original_enter = browser_module.BrowserSession.__aenter__
    async def fixture_enter(session):
        await original_enter(session)
        async def serve(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.abort()
        await session.context.route("**/*", serve)
        return session
    monkeypatch.setattr(browser_module.BrowserSession, "__aenter__", fixture_enter)
    code = main([url, "--authorized", "--headless", "--output", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    if failed:
        assert code == 1
        assert result["error"] == "BLOCKED"
        assert not list(tmp_path.glob("*/*.jpg"))
    else:
        assert code == 0
        assert result["checks"]["full_decode"] is True
        assert result["actual_count"] == (1 if kind == "video" else 2)
        if kind == "gallery":
            with zipfile.ZipFile(Path(result["output_directory"]) / f"{ID}-images.zip") as archive:
                assert archive.testzip() is None
    assert not list(tmp_path.glob(".partial-*"))
