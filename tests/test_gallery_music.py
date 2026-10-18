"""Gallery music is the scoped player's original, verified MP3 bytes."""
import base64
import hashlib
import io
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zlib

import pytest
from playwright.async_api import async_playwright

from lumabox.errors import LumaBoxError


ID = "7695689642767235187"
MUSIC = "https://lf26-music-east.douyinstatic.com/obj/music.mp3?signature=private"


def music_module():
    assert importlib.util.find_spec("lumabox.gallery_music"), "Native gallery music implementation is missing"
    return importlib.import_module("lumabox.gallery_music")


def test_cancelled_gallery_music_creates_nothing(tmp_path):
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3", cancelled=lambda: True)
    assert failure.value.code == "INTERRUPTED"
    assert list(tmp_path.iterdir()) == []


def extract_music(snapshot):
    from lumabox import extract
    implementation = getattr(extract, "extract_gallery_music", None)
    assert callable(implementation), "Scoped background music extraction is missing"
    return implementation(snapshot, ID)


async def gallery_snapshot(defect=None):
    from lumabox.extract import DOM_SNAPSHOT
    html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
    player = '<xg-video-container class="hideXgVideo"><video id="music" src="' + MUSIC + '"></video></xg-video-container>'
    html = html.replace('<div class="basePlayerContainer">', '<div class="basePlayerContainer">' + player)
    html += '<script>Object.defineProperty(document.querySelector("#music"), "duration", {value:28.16});</script>'
    if defect == "wrong_work":
        html = html.replace(ID, "7686430330920075249")
    elif defect == "unknown_work":
        html = html.replace('data-e2e-vid="' + ID + '"', '')
    elif defect == "duplicate":
        html = html.replace(player, player + player)
    elif defect == "slide_only":
        html = html.replace(player, '').replace('<div class="dySwiperSlide">', '<div class="dySwiperSlide">' + player, 1)
    elif defect == "blob":
        html = html.replace(MUSIC, 'blob:unconfirmed')
    elif defect == "missing":
        html = html.replace(player, '')
    elif defect == "unloaded_images":
        html = html.replace('https://p3.douyinpic.com/a.jpeg', '').replace('<span>2</span>', '<span>12</span>')
    # Recommendation media must not interfere with a unique target player.
    html += '<div class="detailrelatedVideoCard"><video src="https://v1.douyinvod.com/recommend.mp4"></video></div>'
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            return await page.evaluate(DOM_SNAPSHOT, {"work_id": ID, "kind": "gallery"})
        finally:
            await browser.close()


async def test_scoped_background_music_does_not_require_all_images():
    assert extract_music(await gallery_snapshot("unloaded_images")) == (MUSIC, 28.16)


@pytest.mark.parametrize("defect,code", [("wrong_work", "AUDIO_FAILED"), ("unknown_work", "AUDIO_FAILED"),
    ("duplicate", "AUDIO_FAILED"), ("slide_only", "NO_AUDIO"), ("blob", "AUDIO_FAILED"), ("missing", "NO_AUDIO")])
async def test_only_unique_confirmed_background_music_is_accepted(defect, code):
    with pytest.raises(LumaBoxError) as failure:
        extract_music(await gallery_snapshot(defect))
    assert failure.value.code == code


def test_audio_host_policy_does_not_expand_video_or_image_hosts():
    from lumabox import urls
    validate = getattr(urls, "validate_audio_url", None)
    assert callable(validate), "Independent audio URL policy is missing"
    assert validate(MUSIC) == MUSIC
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        urls.validate_media_url(MUSIC)
    for url in ["http://lf26-music-east.douyinstatic.com/a.mp3", "https://lf26-music-east.douyinstatic.com.evil.test/a.mp3",
                "https://lf27-music-east.douyinstatic.com/a.mp3", "https://v1.douyinvod.com/a.mp3",
                MUSIC + "#private", "https://lf26-music-east.douyinstatic.com/a%0d.mp3"]:
        with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
            validate(url)


@pytest.fixture(scope="module")
def mp3_fixture(tmp_path_factory):
    path = tmp_path_factory.mktemp("music") / "original.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(path)], check=True, capture_output=True, timeout=30)
    return path


def child_fixture(monkeypatch, source, *, duration=.8, status=None, after_start=None, noisy=False,
                  raw_status=None, after_script=""):
    """Replace only the external browser child, retaining real OS process ownership."""
    module = music_module()
    original = module.ProcessTree
    children = []
    record = status if status is not None else {"ok": True, "duration": duration}
    script = "import sys;from pathlib import Path;sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes());sys.stdout.buffer.flush();"
    if noisy:
        script += "sys.stderr.write('unrelated bounded browser cleanup warning\\n');"
    script += "import base64,zlib;sys.stderr.write(zlib.decompress(base64.b64decode(sys.argv[2])).decode() + '\\n');sys.stderr.flush();" + after_script
    status_text = raw_status if raw_status is not None else "LUMABOX_GALLERY_MUSIC " + json.dumps(record)
    status_arg = base64.b64encode(zlib.compress(status_text.encode())).decode()

    class FixtureTree(original):
        def __enter__(self):
            if "lumabox.gallery_music" in self.command:
                self.command = [sys.executable, "-c", script, str(source), status_arg]
            value = super().__enter__()
            children.append(self.process)
            if after_start:
                after_start(self.command)
            return value

    monkeypatch.setattr(module, "ProcessTree", FixtureTree)
    # Local verification also retains the same process-tree implementation.
    import lumabox.audio as audio
    monkeypatch.setattr(audio, "ProcessTree", FixtureTree)
    return children


def test_native_mp3_bytes_are_preserved_and_fully_verified(tmp_path, mp3_fixture, monkeypatch):
    children = child_fixture(monkeypatch, mp3_fixture, noisy=True)
    target = tmp_path / "original.mp3"
    created = []
    def record(path, identity):
        info = path.stat()
        created.append((path, identity, info.st_size))
    result = music_module().download_gallery_music(ID, target, on_created=record)
    payload = mp3_fixture.read_bytes()
    assert target.read_bytes() == payload
    assert result == {"name": "original.mp3", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                      "width": None, "height": None, "duration": result["duration"], "mime": "audio/mpeg"}
    assert .8 <= result["duration"] <= 1
    assert created == [(target, (target.stat().st_dev, target.stat().st_ino), 0)]
    assert children and all(child.poll() is not None for child in children)
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("status,code", [({"ok": True, "duration": 10}, "VERIFY_FAILED"),
    ({"ok": True, "duration": float("nan")}, "AUDIO_FAILED"),
    ({"ok": False, "code": "NO_AUDIO"}, "NO_AUDIO"),
    ({"ok": False, "code": "UNSAFE_TARGET", "diagnostics": {"network_reason": "DNS_NON_PUBLIC", "host": "localhost"}}, "UNSAFE_TARGET")])
def test_child_failure_or_duration_mismatch_removes_only_owned_file(tmp_path, mp3_fixture, monkeypatch, status, code):
    child_fixture(monkeypatch, mp3_fixture, status=status)
    preserved = tmp_path / "unrelated.mp3"
    preserved.write_bytes(b"keep")
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3")
    assert failure.value.code == code
    assert preserved.read_bytes() == b"keep" and list(tmp_path.iterdir()) == [preserved]


def test_size_limit_reaps_child_and_removes_owned_target(tmp_path, mp3_fixture, monkeypatch):
    children = child_fixture(monkeypatch, mp3_fixture)
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3", max_bytes=64)
    assert failure.value.code == "SIZE_LIMIT"
    assert children and all(child.poll() is not None for child in children)
    assert list(tmp_path.iterdir()) == []


def test_cancel_after_child_start_reaps_it_and_removes_target(tmp_path, mp3_fixture, monkeypatch):
    cancelled = threading.Event()
    children = child_fixture(monkeypatch, mp3_fixture, after_start=lambda _: cancelled.set())
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3", cancelled=cancelled.is_set)
    assert failure.value.code == "INTERRUPTED"
    assert children and all(child.poll() is not None for child in children)
    assert list(tmp_path.iterdir()) == []


def test_total_deadline_reaps_browser_worker(tmp_path, mp3_fixture, monkeypatch):
    import lumabox.audio as audio
    from types import SimpleNamespace
    now = [0.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=audio.time.sleep))
    children = child_fixture(monkeypatch, mp3_fixture, after_start=lambda _: now.__setitem__(0, 901.0))
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3")
    assert failure.value.code == "TIMEOUT"
    assert children and all(child.poll() is not None for child in children)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", ["x" * 300000, 'LUMABOX_GALLERY_MUSIC []',
    'LUMABOX_GALLERY_MUSIC {"ok":true,"duration":' + "9" * 310 + '}',
    'LUMABOX_GALLERY_MUSIC {"ok":true,"duration":true}',
    'unmarked {"ok":true,"duration":0.8}',
    'LUMABOX_GALLERY_MUSIC {"ok":false,"code":"https://private?signature=secret","diagnostics":{"cookie":"secret"}}'],
    ids=["oversized", "wrong_shape", "huge_number", "boolean_duration", "unmarked", "unsafe_fields"])
def test_child_status_is_bounded_validated_and_never_exposed(tmp_path, mp3_fixture, monkeypatch, status):
    children = child_fixture(monkeypatch, mp3_fixture, raw_status=status)
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3")
    assert failure.value.code == "AUDIO_FAILED"
    assert "secret" not in str(failure.value) + json.dumps(failure.value.diagnostics)
    assert children and all(child.poll() is not None for child in children)
    assert list(tmp_path.iterdir()) == []


def test_creation_callback_failure_cleans_its_file_and_propagates(tmp_path):
    error = RuntimeError("recording failed")
    def created(path, identity):
        assert path.stat().st_size == 0
        raise error
    with pytest.raises(RuntimeError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3", on_created=created)
    assert failure.value is error and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("suffix", ["m4a", "mp4", "mp3.exe"])
def test_only_mp3_target_is_accepted(tmp_path, suffix):
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / ("out." + suffix))
    assert failure.value.code == "AUDIO_FAILED" and list(tmp_path.iterdir()) == []


def test_existing_target_is_never_overwritten_or_removed(tmp_path):
    target = tmp_path / "out.mp3"
    target.write_bytes(b"keep")
    with pytest.raises(LumaBoxError):
        music_module().download_gallery_music(ID, target)
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("payload", [b"#EXTM3U\nhttp://127.0.0.1/private\n", b"not MP3", b"ID3" + b"\0" * 100])
def test_non_mp3_payload_is_never_published(tmp_path, monkeypatch, payload):
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    child_fixture(monkeypatch, source)
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3")
    assert failure.value.code == "VERIFY_FAILED"
    assert list(tmp_path.iterdir()) == [source]


def test_mp3_with_duration_header_but_only_half_the_frames_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "partial.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(source)], check=True, capture_output=True, timeout=30)
    payload = source.read_bytes()
    # The Info/Xing header still claims ten seconds. HTTP framing can be valid
    # even though the upstream object contains only half the actual MP3 frames.
    source.write_bytes(payload[:len(payload) // 2])
    child_fixture(monkeypatch, source, duration=10.03)
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music(ID, tmp_path / "out.mp3")
    assert failure.value.code == "VERIFY_FAILED"
    assert list(tmp_path.iterdir()) == [source]


def test_replaced_target_is_never_removed_during_verification(tmp_path, mp3_fixture, monkeypatch):
    target = tmp_path / "out.mp3"
    owned = tmp_path / "detached-owned.mp3"
    def replace(command):
        if command[0] == "ffprobe":
            # The download handle is closed before local verification, including
            # on Windows where an open file cannot be replaced.
            target.rename(owned)
            target.write_bytes(b"replacement")
    child_fixture(monkeypatch, mp3_fixture, after_start=replace)
    with pytest.raises(LumaBoxError):
        music_module().download_gallery_music(ID, target)
    assert target.read_bytes() == b"replacement"


async def test_browser_music_mode_confirms_music_without_loading_images(monkeypatch):
    import lumabox.browser as browser_module
    from lumabox.urls import parse_input
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    html = Path("tests/fixtures/gallery.html").read_text(encoding="utf-8")
    html = html.replace('<div class="basePlayerContainer">', '<div class="basePlayerContainer"><xg-video-container class="hideXgVideo"><video id="music" src="' + MUSIC + '"></video></xg-video-container>')
    html = html.replace('<span>2</span>', '<span>12</span>')
    html += '<script>Object.defineProperty(document.querySelector("#music"),"duration",{value:28.16});</script>'
    async with browser_module.BrowserSession(headless=True) as session:
        await session.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
            if route.request.resource_type == "document" else route.abort())
        loader = getattr(session, "load_gallery_music", None)
        assert callable(loader), "The music browser mode is missing"
        assert await loader(parse_input(f"https://www.douyin.com/note/{ID}")) == (MUSIC, 28.16)


def transport_fixture(monkeypatch, responses):
    """Replace HTTPS transport only; exercise actual policy, body bounds and framing."""
    module = music_module()
    calls = []
    class Socket:
        def settimeout(self, timeout):
            pass
        def shutdown(self, how):
            pass
    class Response:
        def __init__(self, spec):
            self.status = spec.get("status", 200)
            self.headers = {"Content-Type": "audio/mpeg", **spec.get("headers", {})}
            self.body = io.BytesIO(spec.get("body", b"ID3test"))
        def getheader(self, name, default=None):
            return self.headers.get(name, default)
        def read1(self, size):
            return self.body.read(size)
    class Connection:
        def __init__(self, host, ip, timeout):
            calls.append((host, ip))
            self.sock = Socket()
        def request(self, method, path, *, headers):
            assert method == "GET" and headers["Accept-Encoding"] == "identity" and "Cookie" not in headers
        def getresponse(self):
            return Response(responses.pop(0))
        def close(self):
            pass
    monkeypatch.setattr(module, "resolve_public", lambda host, **kwargs: ("93.184.216.34",))
    monkeypatch.setattr(module, "PinnedHTTPSConnection", Connection)
    return calls


def stream_music(output, limit=100):
    module = music_module()
    return module._stream_music(MUSIC, output, {"Accept-Encoding": "identity"}, limit)


def test_stream_transfers_exact_native_bytes_after_validated_redirect(monkeypatch):
    calls = transport_fixture(monkeypatch, [{"status": 302, "headers": {"Location": "/other.mp3"}},
        {"body": b"ID3native", "headers": {"Content-Type": "application/octet-stream", "Content-Length": "9"}}])
    output = io.BytesIO()
    assert stream_music(output) == 9
    assert output.getvalue() == b"ID3native"
    assert calls == [("lf26-music-east.douyinstatic.com", "93.184.216.34")] * 2


@pytest.mark.parametrize("spec,code", [
    ({"status": 302, "headers": {"Location": "https://127.0.0.1/private?token=secret"}}, "UNSAFE_TARGET"),
    ({"status": 206}, "DOWNLOAD_FAILED"),
    ({"headers": {"Content-Type": "audio/mp4"}}, "UNSUPPORTED_MEDIA"),
    ({"headers": {"Content-Length": "101"}}, "SIZE_LIMIT"),
    ({"headers": {"Content-Length": "8"}, "body": b"short"}, "DOWNLOAD_FAILED"),
    ({"headers": {"Content-Length": "-1"}}, "DOWNLOAD_FAILED"),
    ({"body": b"x" * 101}, "SIZE_LIMIT"),
    ({"body": b""}, "DOWNLOAD_FAILED"),
    ({"headers": {"Content-Encoding": "gzip"}}, "UNSUPPORTED_MEDIA"),
])
def test_transfer_rejects_unsafe_incomplete_or_wrong_media(monkeypatch, spec, code):
    calls = transport_fixture(monkeypatch, [spec])
    with pytest.raises(LumaBoxError) as failure:
        stream_music(io.BytesIO())
    assert failure.value.code == code
    assert len(calls) == 1
    assert all(secret not in str(failure.value) + json.dumps(failure.value.diagnostics) for secret in ["token=", "secret", "/private"])
