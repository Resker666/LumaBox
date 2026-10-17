from pathlib import Path
import pytest
from playwright.sync_api import expect
from test_daily_ui import daily_server, browser, wait_finished
from web_helpers import URL


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_hidden_collection_can_restore_and_delete_with_confirmation(daily_server, browser):
    url, worker = daily_server
    job = worker.store.create(URL); saved = wait_finished(worker.store, job["id"])
    path = worker.store.file_path(worker.store.file(saved["files"][0]["id"]))
    original = path.read_bytes(); worker.store.remove(job["id"])
    page = browser.new_page(viewport={"width": 390, "height": 844})
    purges = []
    page.on("request", lambda request: purges.append(request.url) if request.url.endswith("/purge") else None)
    try:
        page.goto(url)
        expect(page.get_by_text("系统可用空间", exact=True)).to_be_visible()
        page.get_by_role("button", name="文件管理", exact=True).click()
        page.get_by_label("文件范围").select_option("hidden")
        card = page.locator(f'[data-storage-job-id="{job["id"]}"]')
        expect(card.get_by_text("记录已移除，文件保留", exact=True)).to_be_visible()
        card.get_by_role("button", name="恢复收藏", exact=True).click()
        expect(card).to_have_count(0)
        page.get_by_label("文件范围").select_option("all")
        expect(card).to_have_count(1)
        assert worker.store.get(job["id"])["status"] == "success"
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="仅移除记录", exact=True).click()
        assert path.read_bytes() == original
        expect(card.get_by_text("记录已移除，文件保留", exact=True)).to_be_visible()
        messages = []
        def cancel(dialog): messages.append(dialog.message); dialog.dismiss()
        page.once("dialog", cancel)
        card.get_by_role("button", name="删除记录及文件", exact=True).click()
        assert not purges and path.read_bytes() == original
        assert job["work_id"] in messages[0] and "无法从光匣恢复" in messages[0]
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="删除记录及文件", exact=True).click()
        expect(card).to_have_count(0)
        assert not path.exists() and len(purges) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=".superpowers/sdd/2026-10-17-retry-storage/storage-mobile.png", full_page=True)
    finally:
        page.close()


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_storage_unknown_content_refusal_and_retry(daily_server, browser):
    url, worker = daily_server
    job = worker.store.create(URL); saved = wait_finished(worker.store, job["id"])
    path = worker.store.file_path(worker.store.file(saved["files"][0]["id"]))
    extra = path.parent / "my-notes.txt"; extra.write_text("keep")
    page = browser.new_page()
    try:
        page.goto(url); page.get_by_role("button", name="文件管理", exact=True).click()
        card = page.locator(f'[data-storage-job-id="{job["id"]}"]')
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="删除记录及文件", exact=True).click()
        expect(page.locator("#storage-error")).to_contain_text("无法确认归属")
        assert path.exists() and extra.read_text() == "keep"
        extra.unlink()
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="删除记录及文件", exact=True).click()
        expect(card).to_have_count(0)
        assert not path.exists()
    finally:
        page.close()
