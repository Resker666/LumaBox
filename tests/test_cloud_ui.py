"""Server storage must be presented as a real client download, not a local folder."""
import zipfile

import pytest
from playwright.sync_api import expect

from test_daily_ui import daily_server, browser
from test_web_ui import submit
from web_helpers import URL


def test_remote_cli_still_binds_only_loopback(tmp_path, monkeypatch):
    import uvicorn
    from fastapi.testclient import TestClient
    from lumabox.web.__main__ import main
    observed = {}
    def inspect_app(app, **options):
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        try:
            observed.update(client.get("/api/session").json(), bind_host=options["host"])
        finally:
            client.close()
    monkeypatch.setattr(uvicorn, "run", inspect_app)
    try:
        code = main(["--remote", "--data", str(tmp_path / "data")])
    except SystemExit as error:
        code = error.code
    assert code == 0, "--remote must construct the existing private service"
    assert observed["remote_mode"] is True
    assert observed["bind_host"] == "127.0.0.1"


@pytest.mark.parametrize("daily_server", [True], indirect=True)
def test_remote_video_and_gallery_are_downloadable_without_local_folder_action(daily_server, browser, media_fixtures, tmp_path):
    url, worker = daily_server
    context = browser.new_context(accept_downloads=True, viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_label("作品链接或分享文案")).to_be_visible()
        submit(page, URL)
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        expect(page.get_by_role("button", name="打开文件夹", exact=True)).to_have_count(0)
        expect(page.get_by_text("已保存到服务器", exact=False)).to_be_visible()
        assert page.request.get(url + "/api/session").json()["remote_mode"] is True
        with page.expect_download() as download:
            page.get_by_role("link", name="下载视频", exact=True).click()
        video = tmp_path / "client.mp4"
        download.value.save_as(video)
        assert video.read_bytes() == media_fixtures[0].read_bytes()
        worker.mode, worker.fixture = "gallery", media_fixtures[1]
        submit(page, "https://v.douyin.com/test/")
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_have_count(2, timeout=10000)
        with page.expect_download() as download:
            page.get_by_role("link", name="下载整组 ZIP", exact=True).click()
        archive = tmp_path / "client-images.zip"
        download.value.save_as(archive)
        with zipfile.ZipFile(archive) as content:
            assert content.read("01.jpg") == media_fixtures[1].read_bytes()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()
