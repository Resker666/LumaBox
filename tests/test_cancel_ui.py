"""Cancellation and actionable diagnostics in the actual local browser UI."""
from playwright.sync_api import expect

from test_daily_ui import daily_server, browser
from test_web_ui import submit
from web_helpers import URL


def test_cancel_pending_and_running_then_retry_without_stopping_service(daily_server, browser):
    url, worker = daily_server
    worker.mode = "timeout"
    context = browser.new_context(viewport={"width": 1100, "height": 900})
    page = context.new_page()
    try:
        page.goto(url)
        submit(page, URL)
        expect(page.locator("#active-list").get_by_text("下载中", exact=True)).to_be_visible(timeout=10000)
        first = worker.store.list()[0]["id"]
        submit(page, "https://v.douyin.com/following/")
        expect(page.locator("#active-list").get_by_text("待下载", exact=True)).to_be_visible()
        pending = worker.store.list()[0]["id"]
        waiting_card = page.locator(f'[data-job-id="{pending}"]')
        waiting_card.get_by_role("button", name="取消下载", exact=True).click()
        expect(waiting_card.get_by_text("已取消", exact=True)).to_be_visible()
        assert worker.store.get(first)["status"] == "running"
        active_card = page.locator(f'[data-job-id="{first}"]')
        active_card.get_by_role("button", name="取消下载", exact=True).click()
        expect(active_card.get_by_text("已取消", exact=True)).to_be_visible(timeout=12000)
        expect(active_card.get_by_role("button", name="取消下载", exact=True)).to_have_count(0)
        expect(active_card.get_by_role("button", name="重新下载", exact=True)).to_be_visible()
        worker.mode = "success"
        active_card.get_by_role("button", name="重新下载", exact=True).click()
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=10000)
        page.get_by_label("任务状态").select_option("cancelled")
        expect(page.locator(".job-card")).to_have_count(2)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_network_reason_is_visible_without_exposing_raw_errors(daily_server, browser):
    url, worker = daily_server
    for index, reason in enumerate(("DNS_NON_PUBLIC", "DNS_LOOKUP_FAILED", "CONNECTION_REFUSED", "CONNECTION_RESET", "CONNECTION_TIMEOUT", "TLS_ERROR")):
        job = worker.store.create(f"https://www.douyin.com/video/{7686430330920075200 + index}")
        worker.store.fail(job["id"], "UNSAFE_TARGET" if reason.startswith("DNS") else "DOWNLOAD_FAILED", {
            "diagnostics": {"network_reason": reason, "stage": "dns_resolution" if reason.startswith("DNS") else "media_transfer",
                            "host": "www.douyin.com", "reason": "secret cookie", "url": "https://secret/"}})
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        page.goto(url)
        expect(page.get_by_text("DNS 返回虚拟或非公网地址", exact=False)).to_be_visible()
        expect(page.get_by_text("DNS 解析失败", exact=False)).to_be_visible()
        expect(page.get_by_text("连接被拒绝", exact=False)).to_be_visible()
        expect(page.get_by_text("连接被重置", exact=False)).to_be_visible()
        expect(page.get_by_text("连接超时", exact=False)).to_be_visible()
        expect(page.get_by_text("TLS 安全连接失败", exact=False)).to_be_visible()
        assert "secret" not in page.locator("body").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()
