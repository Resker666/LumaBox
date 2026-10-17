"""CLI JSON remains portable across Windows console encodings."""
import json
import os
import subprocess
import sys

import pytest
from playwright.sync_api import expect

from lumabox.web.store import JobStore
from test_daily_ui import daily_server, browser
from web_helpers import URL, fixture_worker


@pytest.mark.parametrize("encoding", ["gbk", "ascii"])
@pytest.mark.parametrize("title", ["每日中文标题", "中文标题与表情 🎵"])
def test_cli_json_round_trips_in_non_utf8_stdout(tmp_path, encoding, title):
    result = {"status": "success", "title": title, "output_directory": str(tmp_path / "中文目录")}
    script = tmp_path / "cli_encoding.py"
    script.write_text(
        "import json\nimport lumabox.cli as cli\n"
        f"result = json.loads({json.dumps(result)!r})\n"
        "async def download(*args, **kwargs): return result\n"
        "cli.run_download = download\n"
        f"raise SystemExit(cli.main(['--authorized', {URL!r}]))\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONUTF8": "0"}
    process = subprocess.run([sys.executable, str(script)], capture_output=True, env=environment, timeout=20)
    assert process.returncode == 0, process.stderr.decode("ascii", errors="replace")
    assert json.loads(process.stdout.decode("utf-8")) == result


def test_real_cli_chinese_report_publishes_video_then_optional_audio(web_config, tmp_path, media_fixtures):
    store = JobStore(web_config)
    job = store.create(URL, include_audio=True)
    worker = fixture_worker(web_config, store, tmp_path, media_fixtures[0])
    source = worker.script.read_text(encoding="utf-8")
    source = source.replace("(dest/'report.json').write_text", "result['title']='每日中文标题 🎵'\n(dest/'report.json').write_text")
    source = source.replace("print(json.dumps(result))", """
import lumabox.cli as cli
sys.stdout.reconfigure(encoding='gbk', errors='strict')
async def download(*args, **kwargs): return result
cli.run_download=download
raise SystemExit(cli.main(['--authorized', 'https://www.douyin.com/video/'+work_id]))
""")
    worker.script.write_text(source, encoding="utf-8")
    assert worker.run_once()
    published = store.get(job["id"])
    assert published["status"] == "success"
    assert published["title"] == "每日中文标题 🎵"
    assert published["audio"]["status"] == "pending"
    assert worker.run_once()
    finished = store.get(job["id"])
    assert finished["status"] == "success" and finished["audio"]["status"] == "success"
    assert [file["mime"] for file in finished["files"]] == ["video/mp4", "audio/mp4"]


@pytest.mark.parametrize("payload", [
    b"", b"not-json private-url?signature=secret", b"[]",
    '{"status":"success","title":"中文"}'.encode("gbk"),
    b"x" * (256 * 1024 + 1), b"[" * 2000 + b"]" * 2000,
], ids=["empty", "invalid-json", "non-object", "legacy-gbk", "oversize", "too-deep"])
def test_invalid_cli_result_is_distinct_from_media_failure(web_config, tmp_path, media_fixtures, payload):
    store = JobStore(web_config)
    job = store.create(URL, include_audio=True)
    worker = fixture_worker(web_config, store, tmp_path, media_fixtures[0])
    worker.script.write_text(f"import sys\nsys.stdout.buffer.write({payload!r})\n", encoding="utf-8")
    assert worker.run_once()
    failed = store.get(job["id"])
    assert failed["status"] == "failed" and failed["error"] == "RESULT_PARSE_FAILED"
    assert failed["attempt_count"] == 1 and failed["files"] == []
    assert failed["audio"]["status"] == "none"
    assert "secret" not in json.dumps(failed)
    assert not (web_config.output_dir / job["id"]).exists()
    following = store.create(URL)
    assert fixture_worker(web_config, store, tmp_path, media_fixtures[0]).run_once()
    assert store.get(following["id"])["status"] == "success"


@pytest.mark.parametrize("browser", ["chromium", "webkit"], indirect=True)
def test_result_parse_error_and_media_verification_have_distinct_messages(daily_server, browser):
    url, worker = daily_server
    worker.stop()
    first = worker.store.create(URL)
    worker.store.fail(first["id"], "RESULT_PARSE_FAILED")
    second = worker.store.create("https://v.douyin.com/another-work/")
    worker.store.fail(second["id"], "VERIFY_FAILED")
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.goto(url)
        expect(page.locator(f'[data-job-id="{first["id"]}"]')).to_contain_text(
            "下载结果读取失败，请重试；此错误不代表媒体文件损坏。")
        expect(page.locator(f'[data-job-id="{second["id"]}"]')).to_contain_text(
            "文件完整性检查未通过，请重新下载。")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()
