"""Mobile-first task visibility and optional audio through the real browser UI."""
from pathlib import Path
import json
import time
import pytest

from playwright.sync_api import expect

from test_daily_ui import browser, daily_server, wait_finished
from test_web_ui import submit
from web_helpers import URL


def capture(page, name):
    directory = Path("Downloaded/mobile-audio-validation")
    directory.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(directory / name), full_page=False)


def test_mobile_options_are_optional_and_running_task_stays_in_first_screen(daily_server, browser):
    # Catches a restored hero/storage panel pushing progress below the first screen,
    # or optional audio/limit controls displacing the primary download flow.
    url, worker = daily_server
    worker.mode = "timeout"
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    try:
        page.goto(url)
        expect(page.get_by_role("button", name="文件管理", exact=True)).to_be_in_viewport()
        expect(page.get_by_label("单视频上限")).to_be_hidden()
        expect(page.get_by_label("同时保存音频")).to_be_hidden()
        expect(page.get_by_text("系统可用空间", exact=True)).to_be_hidden()
        assert page.get_by_label("作品链接或分享文案").bounding_box()["y"] < 200
        page.get_by_text("下载选项", exact=True).click()
        expect(page.get_by_label("同时保存音频")).not_to_be_checked()
        expect(page.get_by_label("单视频上限")).to_have_value("1024")
        page.get_by_text("下载选项", exact=True).click()
        submit(page, URL)
        status = page.locator("#active-list").get_by_text("下载中", exact=True)
        expect(status).to_be_visible(timeout=10000)
        page.evaluate("window.scrollTo(0, 0)")
        expect(status).to_be_in_viewport()
        expect(page.get_by_role("button", name="取消下载", exact=True)).to_be_in_viewport()
        assert page.locator("#active-tasks").bounding_box()["y"] < page.locator(".collection").bounding_box()["y"]
        capture(page, "running-mobile-390.png")
        page.get_by_label("搜索收藏").fill("unrelated title")
        page.get_by_label("任务状态").select_option("success")
        expect(status).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        capture(page, "active-with-filter-mobile-390.png")
    finally:
        page.close()


def test_audio_choice_is_explicit_in_each_new_download(daily_server, browser):
    # Catches audio being enabled by default or a checked choice omitted from POST.
    url, worker = daily_server
    worker.mode = "timeout"
    page = browser.new_page()
    requests = []
    page.on("request", lambda request: requests.append(request.post_data_json)
            if request.method == "POST" and request.url == url + "/api/jobs" else None)
    try:
        page.goto(url)
        submit(page, URL)
        assert requests[0].get("include_audio") is False
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("同时保存音频").check()
        submit(page, "https://v.douyin.com/another-work/")
        assert requests[1].get("include_audio") is True
    finally:
        page.close()


def test_audio_activity_and_failure_do_not_replace_video_success(daily_server, browser):
    # Catches deriving the parent status from audio and hiding a cancellable audio
    # task in More. Override only the audio service boundary while it is busy.
    url, worker = daily_server
    job = worker.store.create(URL)
    complete = wait_finished(worker.store, job["id"])
    state = {"status": "running", "error": None}
    page = browser.new_page(viewport={"width": 390, "height": 844})
    def audio_state(route):
        response = route.fetch()
        payload = response.json()
        for item in payload["jobs"]:
            item["audio"] = dict(state)
            item["include_audio"] = True
        route.fulfill(response=response, body=json.dumps(payload))
    page.route("**/api/jobs?*", audio_state)
    try:
        page.goto(url)
        card = page.locator(f'#workspace-view [data-job-id="{job["id"]}"]')
        expect(card.get_by_text("已完成", exact=True)).to_be_visible()
        expect(card.get_by_text("正在提取音频…", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="取消音频", exact=True)).to_be_visible()
        state.update(status="failed", error="NO_AUDIO")
        expect(card.get_by_text("此视频没有音轨，视频已保留。", exact=True)).to_be_visible(timeout=6000)
        expect(card.get_by_text("已完成", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="预览视频", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="取消音频", exact=True)).to_have_count(0)
        assert complete["files"]
    finally:
        page.close()


def test_history_keeps_secondary_actions_and_technical_details_under_more(daily_server, browser):
    # Catches leaking long paths/URLs into compact history or losing folder access.
    url, worker = daily_server
    job = worker.store.create(URL)
    complete = wait_finished(worker.store, job["id"])
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    try:
        page.goto(url)
        card = page.locator(f'#workspace-view [data-job-id="{job["id"]}"]')
        expect(card.get_by_role("button", name="预览视频", exact=True)).to_be_visible()
        expect(card.get_by_role("link", name="下载视频", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="打开文件夹", exact=True)).to_be_hidden()
        expect(card.get_by_text(URL, exact=True)).to_be_hidden()
        expect(card.get_by_text(complete["output_directory"], exact=False)).to_be_hidden()
        card.get_by_text("更多", exact=True).click()
        expect(card.get_by_role("button", name="打开文件夹", exact=True)).to_be_visible()
        expect(card.get_by_role("button", name="提取音频", exact=True)).to_be_visible()
        card.get_by_text("任务详情", exact=True).click()
        expect(card.get_by_text(URL, exact=True)).to_be_visible()
        expect(card.get_by_text(complete["output_directory"], exact=False)).to_be_visible()
        card.get_by_text("更多", exact=True).click()
        capture(page, "history-mobile-390.png")
        page.set_viewport_size({"width": 1440, "height": 1000})
        capture(page, "history-desktop-1440.png")
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="文件管理", exact=True).click()
        expect(page.get_by_text("系统可用空间", exact=True)).to_be_visible()
        capture(page, "management-mobile.png")
    finally:
        page.close()


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_existing_video_audio_can_be_previewed_downloaded_and_released(daily_server, browser, tmp_path):
    # Catches a mismatched extraction endpoint, unusable audio player or audio
    # download, and closing the player without releasing its source.
    url, worker = daily_server
    job = worker.store.create(URL)
    complete = wait_finished(worker.store, job["id"])
    context = browser.new_context(viewport={"width": 390, "height": 844}, accept_downloads=True)
    page = context.new_page()
    try:
        page.goto(url)
        card = page.locator(f'#workspace-view [data-job-id="{job["id"]}"]')
        card.get_by_text("更多", exact=True).click()
        card.get_by_role("button", name="提取音频", exact=True).click()
        expect(card.get_by_text("音频已保存 · M4A", exact=True)).to_be_visible(timeout=12000)
        expect(card.get_by_text("已完成", exact=True)).to_be_visible()
        audio = next(file for file in worker.store.get(job["id"])["files"] if file["mime"] == "audio/mp4")
        with page.expect_download() as download:
            card.get_by_role("link", name="下载音频", exact=True).click()
        saved = tmp_path / "audio.m4a"
        download.value.save_as(saved)
        assert saved.read_bytes() == worker.store.file_path(worker.store.file(audio["id"])).read_bytes()
        assert saved.stat().st_size > 0
        card.get_by_role("button", name="试听音频", exact=True).click()
        player = page.locator("#preview-audio")
        expect(player).to_be_visible()
        expect(player).to_have_attribute("controls", "")
        expect(player).to_have_attribute("src", f'/api/files/{audio["id"]}/preview')
        page.wait_for_function("() => document.querySelector('#preview-audio').readyState >= 1")
        assert player.evaluate("node => node.duration") > .3
        player.evaluate("node => node.play()")
        page.wait_for_function("() => document.querySelector('#preview-audio').currentTime > 0.1")
        expect(page.locator("#preview-video")).to_be_hidden()
        capture(page, "audio-preview-mobile-390.png")
        page.get_by_role("button", name="关闭预览", exact=True).click()
        expect(player).not_to_have_attribute("src", f'/api/files/{audio["id"]}/preview')
        assert worker.store.get(job["id"])["files"][0] == complete["files"][0]
    finally:
        context.close()


def test_duplicate_with_audio_choice_supplements_existing_video(daily_server, browser):
    # Catches forcing a full video download when only its audio is missing.
    url, worker = daily_server
    job = worker.store.create(URL)
    complete = wait_finished(worker.store, job["id"])
    page = browser.new_page()
    try:
        page.goto(url)
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("同时保存音频").check()
        submit(page, URL)
        page.get_by_role("button", name="补充提取音频", exact=True).click()
        expect(page.get_by_text("音频已保存 · M4A", exact=True)).to_be_visible(timeout=12000)
        assert worker.store.search()["total"] == 1
        assert worker.store.get(job["id"])["files"][0] == complete["files"][0]
    finally:
        page.close()


def test_existing_collection_dialog_updates_after_audio_extraction(daily_server, browser):
    # Catches an existing-collection dialog staying frozen after audio completes.
    url, worker = daily_server
    job = worker.store.create(URL)
    wait_finished(worker.store, job["id"])
    page = browser.new_page()
    page.set_default_timeout(6000)
    try:
        page.goto(url)
        submit(page, URL)
        page.get_by_role("button", name="查看已有任务", exact=True).click()
        card = page.locator("#existing-card")
        card.get_by_text("更多", exact=True).click()
        card.get_by_role("button", name="提取音频", exact=True).click()
        expect(card.get_by_text("音频已保存 · M4A", exact=True)).to_be_visible(timeout=10000)
        expect(card.get_by_role("button", name="试听音频", exact=True)).to_be_visible()
    finally:
        page.close()


def test_running_audio_can_cancel_and_storage_waits_for_it(daily_server, browser, monkeypatch):
    # Catches cancelled audio damaging the parent or exposing destructive file
    # controls while the same files are being processed.
    import lumabox.web.audio as audio_module
    from lumabox.errors import LumaBoxError
    def slow_extract(source, destination, *, cancelled, max_bytes, on_created=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not cancelled():
            time.sleep(.02)
        raise LumaBoxError("INTERRUPTED", "cancelled")
    monkeypatch.setattr(audio_module, "extract_audio", slow_extract)
    url, worker = daily_server
    job = worker.store.create(URL)
    complete = wait_finished(worker.store, job["id"])
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url)
        card = page.locator(f'#workspace-view [data-job-id="{job["id"]}"]')
        card.get_by_text("更多", exact=True).click()
        card.get_by_role("button", name="提取音频", exact=True).click()
        expect(card.get_by_text("正在提取音频…", exact=True)).to_be_visible(timeout=8000)
        expect(card.get_by_role("button", name="移除记录", exact=True)).to_have_count(0)
        capture(page, "audio-running-mobile-390.png")
        page.get_by_role("button", name="文件管理", exact=True).click()
        item = page.locator(f'[data-storage-job-id="{job["id"]}"]')
        expect(item.get_by_text("音频正在处理，完成或取消后可管理", exact=True)).to_be_visible()
        expect(item.get_by_role("button", name="删除记录及文件", exact=True)).to_have_count(0)
        expect(item.get_by_role("button", name="仅移除记录", exact=True)).to_have_count(0)
        capture(page, "audio-management-busy-mobile-390.png")
        page.get_by_role("button", name="关闭文件管理", exact=True).click()
        card.get_by_role("button", name="取消音频", exact=True).click()
        expect(card.get_by_text("音频已取消，视频已保留。", exact=True)).to_be_visible(timeout=8000)
        assert worker.store.get(job["id"])["status"] == "success"
        assert worker.store.get(job["id"])["files"] == complete["files"]
    finally:
        page.close()


def test_new_video_audio_option_saves_video_and_m4a(daily_server, browser):
    # Catches the optional checkbox affecting only the visual state and never
    # producing the extra file through the real worker.
    url, worker = daily_server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url)
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("同时保存音频").check()
        page.get_by_text("下载选项", exact=True).click()
        submit(page, URL)
        expect(page.get_by_text("音频已保存 · M4A", exact=True)).to_be_visible(timeout=12000)
        job = worker.store.search()["jobs"][0]
        assert job["status"] == "success" and job["include_audio"] is True
        assert sorted(file["mime"] for file in job["files"]) == ["audio/mp4", "video/mp4"]
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible()
        capture(page, "audio-saved-mobile-390.png")
    finally:
        page.close()


@pytest.mark.parametrize("failure", [None, "NO_AUDIO", "AUTH_REQUIRED", "UNSAFE_TARGET"])
def test_gallery_audio_preserves_images_and_offers_music_actions(daily_server, browser, media_fixtures, monkeypatch, tmp_path, failure):
    import hashlib
    import subprocess
    import lumabox.web.audio as audio
    from lumabox.errors import LumaBoxError
    music = tmp_path / "fixture.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.5", "-c:a", "libmp3lame", str(music)], check=True)
    payload = music.read_bytes()
    def download(work_id, target, *, headless, cancelled, max_bytes, on_created):
        if failure:
            raise LumaBoxError(failure)
        with target.open("xb") as stream:
            stat = target.stat()
            on_created(target, (stat.st_dev, stat.st_ino))
            stream.write(payload)
        return {"name": target.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "mime": "audio/mpeg", "duration": .55}
    monkeypatch.setattr(audio, "download_gallery_music", download)
    url, worker = daily_server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url)
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_label("同时保存音频").check()
        submit(page, "https://www.douyin.com/note/7686430330920075249")
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible(timeout=12000)
        message = {None: "配乐已保存 · MP3", "NO_AUDIO": "此图集没有可用的背景音乐。 图片已保留。",
                   "AUTH_REQUIRED": "背景音乐需要登录后重试。 图片已保留。",
                   "UNSAFE_TARGET": "配乐地址校验未通过，请检查网络后重试。 图片已保留。"}[failure]
        expect(page.get_by_text(message, exact=True)).to_be_visible(timeout=12000)
        page.get_by_text("更多", exact=True).click()
        expect(page.get_by_role("link", name="下载整组 ZIP", exact=True)).to_be_visible()
        if failure:
            expect(page.get_by_role("button", name="重试配乐", exact=True)).to_be_visible()
        else:
            expect(page.get_by_role("link", name="下载音频", exact=True)).to_have_attribute("download", __import__("re").compile(r".*\.mp3"))
            page.get_by_role("button", name="试听音频", exact=True).click()
            expect(page.locator("#preview-audio")).to_be_visible()
            assert page.locator("#preview-audio").evaluate("el => el.tagName") == "AUDIO"
        assert worker.store.search()["jobs"][0]["status"] == "success"
    finally:
        page.close()


@pytest.mark.parametrize("failure", ["QUEUE_FULL", "AUDIO_UNAVAILABLE", "network"])
def test_existing_dialog_shows_audio_request_failure_inside_modal(daily_server, browser, failure):
    # Catches request errors being written behind an open modal, where its user
    # cannot see the recovery message.
    url, worker = daily_server
    job = worker.store.create(URL)
    wait_finished(worker.store, job["id"])
    page = browser.new_page()
    page.set_default_timeout(6000)
    message = "暂时无法连接光匣，请稍后重试。" if failure == "network" else "音频队列已满，请稍后再试。" if failure == "QUEUE_FULL" else "此视频当前无法提取音频，请检查保存文件。"
    def reject_audio(route):
        if failure == "network":
            route.abort()
        else:
            route.fulfill(status=409, json={"error": failure, "message": message})
    page.route(f"**/api/jobs/{job['id']}/audio", reject_audio)
    try:
        page.goto(url)
        submit(page, URL)
        page.get_by_role("button", name="查看已有任务", exact=True).click()
        dialog = page.locator("#existing-dialog")
        dialog.get_by_text("更多", exact=True).click()
        dialog.get_by_role("button", name="提取音频", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text(message)
        expect(dialog.get_by_role("alert")).to_be_visible()
        expect(dialog).to_be_visible()
        expect(page.locator("#action-error")).to_be_hidden()
        expect(dialog.get_by_role("button", name="提取音频", exact=True)).to_be_enabled()
    finally:
        page.close()


@pytest.mark.parametrize("invalid_limit", ["", "0"])
def test_collapsed_invalid_limit_reopens_options_before_submission(daily_server, browser, invalid_limit):
    # Catches native form validation stopping submission with an invisible
    # invalid field and no explanation or path to repair it.
    url, worker = daily_server
    worker.mode = "timeout"
    page = browser.new_page(viewport={"width": 390, "height": 844})
    submissions = []
    page.on("request", lambda request: submissions.append(request.post_data_json)
            if request.method == "POST" and request.url == url + "/api/jobs" else None)
    try:
        page.goto(url)
        page.get_by_label("作品链接或分享文案").fill(URL)
        page.get_by_text("下载选项", exact=True).click()
        field = page.get_by_label("单视频上限")
        field.fill(invalid_limit)
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_role("button", name="开始下载", exact=True).click()
        expect(field).to_be_visible()
        expect(field).to_be_focused()
        expect(page.locator("#submit-error")).to_contain_text("1～8192 MiB")
        assert submissions == []
        field.fill("1024")
        page.get_by_text("下载选项", exact=True).click()
        page.get_by_role("button", name="开始下载", exact=True).click()
        expect(page.locator("#active-list").get_by_text("下载中", exact=True)).to_be_visible(timeout=8000)
        assert len(submissions) == 1 and submissions[0]["video_limit_mib"] == 1024
        expect(page.locator("#submit-error")).to_be_hidden()
    finally:
        page.close()


def test_existing_dialog_retains_details_focus_when_audio_state_refreshes(daily_server, browser):
    # Catches polling replacing a focused details summary without restoring
    # keyboard focus to the same surviving control.
    url, worker = daily_server
    job = worker.store.create(URL)
    wait_finished(worker.store, job["id"])
    page = browser.new_page()
    state = {"status": "none", "error": None}
    def current_job(route):
        response = route.fetch()
        payload = response.json()
        payload["audio"] = dict(state)
        route.fulfill(response=response, json=payload)
    page.route(f"**/api/jobs/{job['id']}", current_job)
    try:
        page.goto(url)
        submit(page, URL)
        page.get_by_role("button", name="查看已有任务", exact=True).click()
        dialog = page.locator("#existing-dialog")
        dialog.get_by_text("更多", exact=True).click()
        details = dialog.get_by_text("任务详情", exact=True)
        details.click()
        details.focus()
        state.update(status="failed", error="RESULT_UNAVAILABLE")
        expect(dialog.get_by_text("原音频文件已移动或损坏，可重新提取。", exact=True)).to_be_visible(timeout=6000)
        expect(details).to_be_focused()
    finally:
        page.close()


def test_legacy_gallery_audio_offers_manual_music_save(daily_server, browser, media_fixtures):
    url, worker = daily_server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    job = worker.store.create("https://www.douyin.com/note/7686430330920075249")
    wait_finished(worker.store, job["id"])
    page = browser.new_page()
    def legacy(route):
        response = route.fetch()
        payload = response.json()
        if isinstance(payload, dict) and "jobs" in payload:
            for item in payload["jobs"]:
                item["audio"] = {"status": "unsupported", "error": None}
        route.fulfill(response=response, json=payload)
    page.route("**/api/jobs?**", legacy)
    try:
        page.goto(url)
        expect(page.get_by_text("图片已保存，可单独保存配乐。", exact=True)).to_be_visible()
        page.get_by_text("更多", exact=True).click()
        expect(page.get_by_role("button", name="保存配乐", exact=True)).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("error,detail,message", [
    ("UNSAFE_TARGET", {"policy_reason": "AUDIO_HOST_UNSUPPORTED", "host": "music.example.com", "stage": "media_transfer"},
     "暂不支持此配乐域名：music.example.com。 图片已保留。"),
    ("UNSAFE_TARGET", {"network_reason": "DNS_NON_PUBLIC", "host": "lf9.douyinstatic.com"},
     "DNS 返回虚拟或非公网地址，当前地址检查已停止连接。请检查 DNS 或代理设置后重试。 图片已保留。"),
    ("DOWNLOAD_FAILED", {"network_reason": "DNS_LOOKUP_FAILED", "host": "lf26.douyinstatic.com"},
     "DNS 解析失败，无法取得网站地址。请检查网络和 DNS 设置后重试。 图片已保留。"),
    ("DOWNLOAD_FAILED", {"network_reason": "CONNECTION_RESET"},
     "连接被重置，下载连接已中断，请稍后重试。 图片已保留。"),
    ("UNSAFE_TARGET", {"policy_reason": "AUDIO_URL_INVALID"},
     "配乐地址格式无效，无法保存，请稍后重试。 图片已保留。"),
    ("UNSAFE_TARGET", {"policy_reason": "AUDIO_REDIRECT_LIMIT"},
     "配乐地址跳转次数过多，请稍后重试。 图片已保留。"),
])
def test_gallery_music_diagnostics_distinguish_policy_and_network(daily_server, browser, media_fixtures, error, detail, message):
    url, worker = daily_server
    worker.mode, worker.fixture = "gallery", media_fixtures[1]
    job = worker.store.create("https://www.douyin.com/note/7686430330920075249")
    wait_finished(worker.store, job["id"])
    page = browser.new_page(viewport={"width": 390, "height": 844})
    def diagnostic(route):
        response = route.fetch()
        payload = response.json()
        if isinstance(payload, dict) and "jobs" in payload:
            for item in payload["jobs"]:
                item["audio"] = {"status": "failed", "error": error, "detail": detail}
        route.fulfill(response=response, json=payload)
    page.route("**/api/jobs?**", diagnostic)
    try:
        page.goto(url)
        expect(page.get_by_text(message, exact=True)).to_be_visible()
        expect(page.locator("#job-list").get_by_text("已完成", exact=True)).to_be_visible()
        page.get_by_text("更多", exact=True).click()
        expect(page.get_by_role("button", name="重试配乐", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="下载整组 ZIP", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()
