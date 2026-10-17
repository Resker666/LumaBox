import pytest
from playwright.sync_api import expect
from test_daily_ui import daily_server, browser, wait_finished
from test_web_ui import submit
from web_helpers import URL, retry_worker


@pytest.fixture
def retry_server(daily_server, tmp_path, media_fixtures):
    url, worker = daily_server
    retry_worker(worker.config, worker.store, tmp_path, media_fixtures[0])
    return url, worker


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_retry_wait_can_cancel(retry_server, browser):
    url, worker = retry_server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url); submit(page, URL)
        expect(page.locator("#job-list").get_by_text("等待重试", exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("button", name="取消下载", exact=True).click()
        expect(page.locator("#job-list").get_by_text("已取消", exact=True)).to_be_visible()
        assert worker.store.search()["jobs"][0]["attempt_count"] == 1
    finally:
        page.close()


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_saved_work_can_be_restored_from_duplicate_notice(daily_server, browser):
    url, worker = daily_server
    old = worker.store.create(URL); wait_finished(worker.store, old["id"])
    worker.store.remove(old["id"])
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url); submit(page, URL)
        expect(page.get_by_role("button", name="恢复并查看", exact=True)).to_be_visible()
        page.get_by_role("button", name="恢复并查看", exact=True).click()
        expect(page.locator("#existing-card").get_by_text("已完成", exact=True)).to_be_visible()
        assert worker.store.search()["total"] == 1
        assert worker.store.get(old["id"])["status"] == "success"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_explicit_redownload_preserves_old_files(daily_server, browser):
    url, worker = daily_server
    old = worker.store.create(URL); wait_finished(worker.store, old["id"])
    page = browser.new_page()
    try:
        page.goto(url); submit(page, URL)
        expect(page.get_by_role("button", name="重新下载", exact=True)).to_be_visible()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name="重新下载", exact=True).click()
        assert worker.store.search()["total"] == 1
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="重新下载", exact=True).click()
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_have_count(2, timeout=12000)
        assert worker.store.get(old["id"])["files"]
    finally:
        page.close()
