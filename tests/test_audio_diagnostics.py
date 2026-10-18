"""Music CDN rejections and DNS errors remain distinguishable across restarts."""
import json
import threading

import pytest

from lumabox.diagnostics import public_diagnostics
from lumabox.errors import LumaBoxError
from lumabox.extract import extract_gallery_music
from lumabox.urls import validate_audio_url, validate_media_url
from lumabox.web.store import JobStore
from test_audio_jobs import client_for
from test_daily_api import headers
from test_gallery_audio_jobs import saved_gallery, native_music, music_mp3
from test_gallery_music import child_fixture, mp3_fixture, music_module, transport_fixture


def test_observed_lf9_music_node_is_accepted_only_for_audio():
    url = "https://lf9-music-east.douyinstatic.com/obj/music.mp3?signature=private"
    assert validate_audio_url(url) == url
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        validate_media_url(url)


def test_unknown_music_host_reports_only_safe_policy_details():
    snapshot = {"work_id": "7695689642767235187", "kind": "gallery", "scope_confirmed": True,
                "gallery_music": [{"url": "https://lf27-music-west.douyinstatic.com/private.mp3?signature=secret", "duration": 28.16}]}
    with pytest.raises(LumaBoxError) as failure:
        extract_gallery_music(snapshot, "7695689642767235187")
    assert failure.value.code == "UNSAFE_TARGET"
    assert failure.value.diagnostics == {"policy_reason": "AUDIO_HOST_UNSUPPORTED", "stage": "media_extraction",
                                         "host": "lf27-music-west.douyinstatic.com"}
    assert "signature" not in json.dumps(failure.value.diagnostics)


@pytest.mark.parametrize("url", [
    "http://lf9-music-east.douyinstatic.com/private.mp3?signature=secret",
    "https://lf26-music-east.douyinstatic.com/private.mp3?signature=secret#fragment",
    "https://username:password@lf9-music-east.douyinstatic.com/private.mp3",
])
def test_malformed_music_url_has_a_fixed_reason_without_private_url(url):
    with pytest.raises(LumaBoxError) as failure:
        validate_audio_url(url)
    detail = public_diagnostics(failure.value.diagnostics)
    assert detail["policy_reason"] == "AUDIO_URL_INVALID"
    assert not any(value in json.dumps(detail) for value in ("signature", "secret", "username", "password", "/private"))


def test_policy_diagnostics_keep_bounded_reason_host_and_stage_only():
    detail = public_diagnostics({"policy_reason": "AUDIO_HOST_UNSUPPORTED", "stage": "media_transfer",
        "host": "lf27-music-west.douyinstatic.com", "url": "https://example/private?signature=secret", "cookie": "secret"})
    assert detail == {"policy_reason": "AUDIO_HOST_UNSUPPORTED", "stage": "media_transfer", "host": "lf27-music-west.douyinstatic.com"}
    assert public_diagnostics({"policy_reason": "secret", "stage": "media_transfer", "host": "example.com"}) == {}
    assert public_diagnostics({"policy_reason": "AUDIO_URL_INVALID", "stage": "secret", "host": "bad.example/?signature=secret"}) == {"policy_reason": "AUDIO_URL_INVALID"}


def test_music_redirect_limit_has_a_specific_safe_reason(monkeypatch):
    import io
    transport_fixture(monkeypatch, [{"status": 302, "headers": {"Location": "/loop.mp3?signature=secret"}} for _ in range(6)])
    with pytest.raises(LumaBoxError) as failure:
        music_module()._stream_music("https://lf26-music-east.douyinstatic.com/obj/music.mp3", io.BytesIO(), {"Accept-Encoding": "identity"}, 100)
    assert failure.value.code == "UNSAFE_TARGET"
    assert failure.value.diagnostics == {"policy_reason": "AUDIO_REDIRECT_LIMIT", "stage": "media_transfer", "host": "lf26-music-east.douyinstatic.com"}
    assert "secret" not in json.dumps(failure.value.diagnostics)


def test_music_redirect_without_location_reports_invalid_url(monkeypatch):
    import io
    transport_fixture(monkeypatch, [{"status": 302}])
    with pytest.raises(LumaBoxError) as failure:
        music_module()._stream_music("https://lf26-music-east.douyinstatic.com/obj/music.mp3", io.BytesIO(), {"Accept-Encoding": "identity"}, 100)
    assert failure.value.code == "UNSAFE_TARGET"
    assert failure.value.diagnostics == {"policy_reason": "AUDIO_URL_INVALID", "stage": "media_transfer", "host": "lf26-music-east.douyinstatic.com"}


def test_verified_music_redirect_is_followed_but_not_a_new_player_source(monkeypatch):
    import io
    url = "https://v9-default.tt.x.bsgslb.cn/private.mp3?signature=secret"
    calls = transport_fixture(monkeypatch, [{"status": 302, "headers": {"Location": url}},
        {"body": b"ID3native", "headers": {"Content-Length": "9"}}])
    output = io.BytesIO()
    assert music_module()._stream_music("https://lf9-music-east.douyinstatic.com/obj/music.mp3", output, {"Accept-Encoding": "identity"}, 100) == 9
    assert output.getvalue() == b"ID3native"
    assert calls == [("lf9-music-east.douyinstatic.com", "93.184.216.34"), ("v9-default.tt.x.bsgslb.cn", "93.184.216.34")]
    for validate in (validate_audio_url, validate_media_url):
        with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
            validate(url)


def test_policy_detail_survives_real_music_child_status(tmp_path, mp3_fixture, monkeypatch):
    detail = {"policy_reason": "AUDIO_HOST_UNSUPPORTED", "host": "lf27-music-west.douyinstatic.com", "stage": "media_extraction"}
    child_fixture(monkeypatch, mp3_fixture, status={"ok": False, "code": "UNSAFE_TARGET", "diagnostics": {
        **detail, "url": "https://example.com/private?signature=secret", "cookie": "secret"}})
    target = tmp_path / "out.mp3"
    with pytest.raises(LumaBoxError) as failure:
        music_module().download_gallery_music("7695689642767235187", target)
    assert failure.value.diagnostics == detail
    assert not target.exists()


@pytest.mark.parametrize("detail", [
    {"policy_reason": "AUDIO_HOST_UNSUPPORTED", "host": "lf27-music-west.douyinstatic.com", "stage": "media_extraction"},
    {"network_reason": "DNS_NON_PUBLIC", "host": "www.douyin.com", "stage": "dns_resolution"},
    {"network_reason": "DNS_LOOKUP_FAILED", "host": "lf9-music-east.douyinstatic.com", "stage": "dns_resolution"},
])
def test_audio_failure_detail_survives_api_and_restart_then_clears_on_retry(web_config, tmp_path, media_fixtures, music_mp3, monkeypatch, detail):
    import lumabox.web.audio as audio
    store = JobStore(web_config)
    job, originals = saved_gallery(store, tmp_path, media_fixtures, selected=True)

    def rejected(*args, **kwargs):
        raise LumaBoxError("UNSAFE_TARGET", "Music address rejected", diagnostics={**detail,
            "url": "https://example.com/private?signature=secret", "Cookie": "secret"})

    monkeypatch.setattr(audio, "download_gallery_music", rejected)
    assert store.audio.run_once(threading.Event())
    reopened = JobStore(web_config)
    reopened.recover()
    with client_for(reopened) as client:
        failed = client.get(f'/api/jobs/{job["id"]}').json()
        assert failed["audio"] == {"status": "failed", "error": "UNSAFE_TARGET", "detail": detail}
        assert failed["status"] == "success" and len(failed["files"]) == len(originals)
        assert "secret" not in json.dumps(failed)
        pending = client.post(f'/api/jobs/{job["id"]}/audio', headers=headers(client), json={}).json()
        assert pending["audio"] == {"status": "pending", "error": None}
    native_music(monkeypatch, music_mp3.read_bytes())
    assert reopened.audio.run_once(threading.Event())
    assert reopened.get(job["id"])["audio"] == {"status": "success", "error": None}
    for file_id, data in originals.items():
        assert reopened.file_path(reopened.file(file_id)).read_bytes() == data
