"""Local extraction preserves AAC packets and publishes only verified audio."""
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import sys
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lumabox.errors import LumaBoxError


def extract_audio(*args, **kwargs):
    spec = importlib.util.find_spec("lumabox.audio")
    implementation = getattr(importlib.import_module("lumabox.audio"), "extract_audio", None) if spec else None
    assert callable(implementation), "The local video audio extraction primitive is missing"
    return implementation(*args, **kwargs)


def audio_staging_path(target):
    implementation = getattr(importlib.import_module("lumabox.audio"), "audio_staging_path", None)
    assert callable(implementation), "The recoverable audio staging path helper is missing"
    return implementation(target)


def probe(path, *options):
    result = subprocess.run(["ffprobe", "-v", "error", *options, "-of", "json", str(path)],
                            check=True, capture_output=True, timeout=30)
    return json.loads(result.stdout)


def packet_hashes(path):
    return [packet["data_hash"] for packet in probe(path, "-select_streams", "a:0", "-show_packets",
            "-show_entries", "packet=data_hash", "-show_data_hash", "sha256")["packets"]]


def test_aac_packets_are_preserved_in_a_real_decodable_m4a(tmp_path, media_fixtures):
    source = media_fixtures[0]
    original = source.read_bytes()
    target = tmp_path / "声音.m4a"
    result = extract_audio(source, target)

    assert result == {"name": target.name, "bytes": target.stat().st_size,
                      "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                      "width": None, "height": None, "duration": result["duration"], "mime": "audio/mp4"}
    assert math.isfinite(result["duration"]) and 0 < result["duration"] < 1
    streams = probe(target, "-show_streams")["streams"]
    assert len(streams) == 1 and streams[0]["codec_type"] == "audio" and streams[0]["codec_name"] == "aac"
    assert packet_hashes(target) == packet_hashes(source)
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(target),
                    "-map", "0:a:0", "-f", "null", "-"], check=True, capture_output=True, timeout=30)
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_non_aac_audio_is_transcoded_to_aac(tmp_path, media_fixtures):
    source = tmp_path / "mp3-video.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(media_fixtures[0]),
                    "-c:v", "copy", "-c:a", "libmp3lame", "-b:a", "128k", str(source)],
                   check=True, capture_output=True, timeout=30)
    original = source.read_bytes()
    target = tmp_path / "converted.m4a"
    result = extract_audio(source, target)
    stream = probe(target, "-show_streams")["streams"][0]
    assert stream["codec_name"] == "aac" and result["duration"] > 0
    assert source.read_bytes() == original


def test_silent_video_has_clean_no_audio_error(tmp_path, media_fixtures):
    source = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(media_fixtures[0]),
                    "-c:v", "copy", "-an", str(source)], check=True, capture_output=True, timeout=30)
    original = source.read_bytes()
    target = tmp_path / "silent.m4a"
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, target)
    assert failure.value.code == "NO_AUDIO" and failure.value.diagnostics == {}
    assert source.read_bytes() == original and not target.exists()
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("payload", [b"not media", b"\0\0\0\x18ftypmp42" + b"\0" * 64,
                                      b"#EXTM3U\nhttp://127.0.0.1:1/private-token\n"])
def test_invalid_input_is_not_published_or_leaked(tmp_path, payload):
    source = tmp_path / "secret-invalid.mp4"
    source.write_bytes(payload)
    target = tmp_path / "out.m4a"
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, target)
    assert failure.value.code == "AUDIO_FAILED"
    assert "private-token" not in str(failure.value) and str(source) not in str(failure.value)
    assert failure.value.diagnostics == {} and not target.exists()
    assert source.read_bytes() == payload


def test_audio_only_input_is_rejected(tmp_path, media_fixtures):
    source = tmp_path / "audio-only.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(media_fixtures[0]),
                    "-vn", "-c:a", "copy", str(source)], check=True, capture_output=True, timeout=30)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, tmp_path / "out.m4a")
    assert failure.value.code == "AUDIO_FAILED"


def test_existing_target_and_source_are_never_overwritten(tmp_path, media_fixtures):
    source = media_fixtures[0]
    original = source.read_bytes()
    target = tmp_path / "exists.m4a"
    target.write_bytes(b"keep this")
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, target)
    assert failure.value.code == "AUDIO_FAILED"
    assert target.read_bytes() == b"keep this" and source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("name", ["wrong.mp3", "wrong.mp4", "wrong.m4a.exe"])
def test_target_requires_m4a_extension(tmp_path, media_fixtures, name):
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], tmp_path / name)
    assert failure.value.code == "AUDIO_FAILED" and not list(tmp_path.iterdir())


def test_cancel_before_start_preserves_source_and_creates_nothing(tmp_path, media_fixtures):
    source = media_fixtures[0]
    original = source.read_bytes()
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, tmp_path / "out.m4a", cancelled=lambda: True)
    assert failure.value.code == "INTERRUPTED"
    assert source.read_bytes() == original and not list(tmp_path.iterdir())


def test_size_limit_removes_only_owned_output(tmp_path, media_fixtures):
    unrelated = tmp_path / "unrelated.m4a"
    unrelated.write_bytes(b"preserve")
    source = media_fixtures[0]
    original = source.read_bytes()
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, tmp_path / "out.m4a", max_bytes=64)
    assert failure.value.code == "SIZE_LIMIT"
    assert source.read_bytes() == original and unrelated.read_bytes() == b"preserve"
    assert list(tmp_path.iterdir()) == [unrelated]


def test_missing_source_has_fixed_audio_error(tmp_path):
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(tmp_path / "absent.mp4", tmp_path / "out.m4a")
    assert failure.value.code == "AUDIO_FAILED" and not list(tmp_path.iterdir())


def test_target_created_during_extraction_is_preserved(tmp_path, media_fixtures, monkeypatch):
    target = tmp_path / "raced.m4a"
    original_open = Path.open

    def create_competitor(path, mode="r", *args, **kwargs):
        if path == target and mode == "xb":
            with original_open(path, "wb") as competitor:
                competitor.write(b"concurrent target")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_competitor)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], target)
    assert failure.value.code == "AUDIO_FAILED" and target.read_bytes() == b"concurrent target"
    assert list(tmp_path.iterdir()) == [target]


def test_cancel_during_publication_cleans_own_target(tmp_path, media_fixtures, monkeypatch):
    target = tmp_path / "cancelled.m4a"
    cancelled = threading.Event()
    original_open = Path.open

    class CancelAfterWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def write(self, data):
            result = self.stream.write(data)
            cancelled.set()
            return result

    def open_and_cancel(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return CancelAfterWrite(stream) if path == target and mode == "xb" else stream

    monkeypatch.setattr(Path, "open", open_and_cancel)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], target, cancelled=cancelled.is_set)
    assert failure.value.code == "INTERRUPTED" and not list(tmp_path.iterdir())


def watch_processes(monkeypatch, on_started):
    """Observe real managed children while retaining their process-tree cleanup."""
    audio = importlib.import_module("lumabox.audio")
    original_tree = audio.ProcessTree
    children = []

    class ObservedTree(original_tree):
        def __enter__(self):
            value = super().__enter__()
            children.append(self.process)
            on_started(self.command)
            return value

    monkeypatch.setattr(audio, "ProcessTree", ObservedTree)
    return children


@pytest.mark.parametrize("phase", ["source_probe", "extract", "output_probe", "decode"])
def test_cancel_stops_real_children_in_each_media_phase(tmp_path, media_fixtures, monkeypatch, phase):
    cancelled = threading.Event()
    source = media_fixtures[0]
    original = source.read_bytes()

    def on_started(command):
        if command[0] == "ffprobe":
            current = "source_probe" if str(source) in command else "output_probe"
        else:
            current = "decode" if "null" in command else "extract"
        if current == phase:
            cancelled.set()

    children = watch_processes(monkeypatch, on_started)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, tmp_path / "out.m4a", cancelled=cancelled.is_set)
    assert failure.value.code == "INTERRUPTED" and children
    assert all(child.poll() is not None for child in children)
    assert source.read_bytes() == original and not list(tmp_path.iterdir())


def test_cancel_during_hash_prevents_publication(tmp_path, media_fixtures, monkeypatch):
    audio = importlib.import_module("lumabox.audio")
    cancelled = threading.Event()
    original_sha256 = hashlib.sha256

    class CancellingDigest:
        def __init__(self):
            self.digest = original_sha256()

        def update(self, data):
            self.digest.update(data)
            cancelled.set()

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(audio, "hashlib", SimpleNamespace(sha256=CancellingDigest))
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], tmp_path / "out.m4a", cancelled=cancelled.is_set)
    assert failure.value.code == "INTERRUPTED" and not list(tmp_path.iterdir())


def test_total_deadline_stops_and_reaps_the_probe(tmp_path, media_fixtures, monkeypatch):
    audio = importlib.import_module("lumabox.audio")
    elapsed = [0.0]
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=audio.time.sleep))
    children = watch_processes(monkeypatch, lambda command: elapsed.__setitem__(0, 901.0))
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], tmp_path / "out.m4a")
    assert failure.value.code == "TIMEOUT" and children
    assert all(child.poll() is not None for child in children) and not list(tmp_path.iterdir())


def test_missing_ffprobe_is_a_safe_dependency_error(tmp_path, media_fixtures, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], tmp_path / "out.m4a")
    assert failure.value.code == "DEPENDENCY_MISSING" and failure.value.diagnostics == {}
    assert not list(tmp_path.iterdir())


def test_probe_output_is_bounded_even_for_a_real_noisy_process(tmp_path, media_fixtures, monkeypatch):
    audio = importlib.import_module("lumabox.audio")
    original_tree = audio.ProcessTree
    children = []

    class NoisyProbe(original_tree):
        def __enter__(self):
            if self.command[0] == "ffprobe":
                self.command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 300000)"]
            result = super().__enter__()
            children.append(self.process)
            return result

    monkeypatch.setattr(audio, "ProcessTree", NoisyProbe)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], tmp_path / "out.m4a")
    assert failure.value.code == "AUDIO_FAILED" and children
    assert all(child.poll() is not None for child in children) and not list(tmp_path.iterdir())


@pytest.fixture
def network_source(media_fixtures):
    requests = []
    payload = media_fixtures[0].read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/secret", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_fake_mp4_playlist_never_contacts_network(tmp_path, network_source):
    remote, requests = network_source
    source = tmp_path / "remote.mp4"
    # A forged MP4 signature must not let auto-detection switch to HLS.
    source.write_bytes(b"\0\0\0\x18ftypmp42\0\0\0\0mp42isom" +
                       ("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n" + remote + "\n#EXT-X-ENDLIST\n").encode())
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, tmp_path / "out.m4a")
    assert failure.value.code == "AUDIO_FAILED" and requests == []


@pytest.mark.parametrize("phase", ["source_probe", "extract", "output_probe", "decode"])
def test_every_media_process_refuses_http_input(tmp_path, media_fixtures, network_source, monkeypatch, phase):
    audio = importlib.import_module("lumabox.audio")
    original_tree = audio.ProcessTree
    remote, requests = network_source
    source = media_fixtures[0]

    class RedirectedInput(original_tree):
        def __enter__(self):
            if self.command[0] == "ffprobe":
                current = "source_probe" if str(source) in self.command else "output_probe"
            else:
                current = "decode" if "null" in self.command else "extract"
            if current == phase:
                self.command = list(self.command)
                self.command[self.command.index("-i") + 1] = remote
            return super().__enter__()

    monkeypatch.setattr(audio, "ProcessTree", RedirectedInput)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, tmp_path / "out.m4a")
    assert failure.value.code == "AUDIO_FAILED" and requests == []
    assert not list(tmp_path.iterdir())


def test_corrupt_audio_packets_fail_full_decode_before_publication(tmp_path, media_fixtures, monkeypatch):
    audio = importlib.import_module("lumabox.audio")
    original_tree = audio.ProcessTree
    target = tmp_path / "out.m4a"
    staging = audio_staging_path(target)

    class CorruptAudioPackets(original_tree):
        def __exit__(self, *args):
            super().__exit__(*args)
            if self.command[0] == "ffmpeg" and "-c:a" in self.command:
                data = staging.read_bytes()
                payload_start = data.index(b"mdat") + 4
                staging.write_bytes(data[:payload_start] + b"\0" * (len(data) - payload_start))

    monkeypatch.setattr(audio, "ProcessTree", CorruptAudioPackets)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], target)
    assert failure.value.code == "AUDIO_FAILED" and not list(tmp_path.iterdir())


def test_cleanup_failure_does_not_leave_an_unpublished_target(tmp_path, media_fixtures, monkeypatch):
    audio = importlib.import_module("lumabox.audio")
    original_unlink = audio._unlink_owned
    failed_once = []
    target = tmp_path / "out.m4a"
    staging = audio_staging_path(target)

    def fail_first_temporary_cleanup(path, identity):
        if path == staging and not failed_once:
            failed_once.append(True)
            raise PermissionError("simulated file sharing conflict")
        return original_unlink(path, identity)

    monkeypatch.setattr(audio, "_unlink_owned", fail_first_temporary_cleanup)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], target)
    assert failure.value.code == "AUDIO_FAILED" and not list(tmp_path.iterdir())


def test_crashed_extraction_leaves_staging_at_recoverable_path(tmp_path, media_fixtures):
    source = media_fixtures[0]
    original = source.read_bytes()
    target = tmp_path / ".audio-restart-token.m4a"
    staging = audio_staging_path(target)
    script = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[3])
import lumabox.audio as audio
original_tree = audio.ProcessTree
class CrashAfterExtraction(original_tree):
    def __exit__(self, *args):
        super().__exit__(*args)
        if self.command[0] == 'ffmpeg' and '-c:a' in self.command:
            os._exit(77)
audio.ProcessTree = CrashAfterExtraction
audio.extract_audio(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    process = subprocess.run([sys.executable, "-c", script, str(source), str(target),
                              str(Path(__file__).resolve().parents[1] / "src")],
                             capture_output=True, timeout=30)
    assert process.returncode == 77
    assert staging == tmp_path / ".lumabox-.audio-restart-token.m4a.partial.m4a"
    assert list(tmp_path.iterdir()) == [staging] and staging.stat().st_size > 0
    assert not target.exists() and source.read_bytes() == original
    # A recovery owner can locate and remove exactly this interrupted job's file.
    staging.unlink()
    assert not list(tmp_path.iterdir())


def test_existing_staging_is_rejected_without_overwrite_or_cleanup(tmp_path, media_fixtures):
    source = media_fixtures[0]
    original = source.read_bytes()
    target = tmp_path / ".audio-active-token.m4a"
    staging = audio_staging_path(target)
    staging.write_bytes(b"belongs to another extraction")
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, target)
    assert failure.value.code == "AUDIO_FAILED"
    assert staging.read_bytes() == b"belongs to another extraction" and not target.exists()
    assert source.read_bytes() == original and list(tmp_path.iterdir()) == [staging]


def test_creation_callback_records_both_identities_before_any_bytes_are_written(tmp_path, media_fixtures):
    target = tmp_path / ".audio-identities.m4a"
    staging = audio_staging_path(target)
    records = []

    def created(path, identity):
        stat = path.stat()
        assert identity == (stat.st_dev, stat.st_ino)
        assert path.read_bytes() == b""
        records.append((path, identity))

    extract_audio(media_fixtures[0], target, on_created=created)
    assert [path for path, identity in records] == [staging, target]
    assert records[0][1] != records[1][1]
    stat = target.stat()
    assert records[1][1] == (stat.st_dev, stat.st_ino)
    assert target.stat().st_size > 0 and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("competing", ["staging", "target"])
def test_preexisting_paths_are_not_reported_as_owned(tmp_path, media_fixtures, competing):
    target = tmp_path / ".audio-competitor.m4a"
    staging = audio_staging_path(target)
    path = staging if competing == "staging" else target
    path.write_bytes(b"belongs to a different owner")
    records = []
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], target, on_created=lambda *record: records.append(record))
    assert failure.value.code == "AUDIO_FAILED" and records == []
    assert path.read_bytes() == b"belongs to a different owner" and list(tmp_path.iterdir()) == [path]


def test_final_target_race_is_not_reported_as_owned(tmp_path, media_fixtures, monkeypatch):
    target = tmp_path / ".audio-racing-owner.m4a"
    staging = audio_staging_path(target)
    original_open = Path.open
    records = []

    def competing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "xb":
            with original_open(path, "wb") as competitor:
                competitor.write(b"different owner won the race")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_open)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(media_fixtures[0], target, on_created=lambda *record: records.append(record))
    assert failure.value.code == "AUDIO_FAILED"
    assert [path for path, identity in records] == [staging]
    assert target.read_bytes() == b"different owner won the race" and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("phase", ["staging", "target"])
@pytest.mark.parametrize("exception_type", [RuntimeError, OSError])
def test_callback_failure_propagates_and_cleans_only_created_files(tmp_path, media_fixtures, phase, exception_type):
    target = tmp_path / ".audio-callback-failure.m4a"
    failing_path = audio_staging_path(target) if phase == "staging" else target
    unrelated = tmp_path / "unrelated.m4a"
    unrelated.write_bytes(b"preserve unrelated audio")
    source = media_fixtures[0]
    original = source.read_bytes()
    sentinel = exception_type("creation manifest could not be stored")

    def created(path, identity):
        if path == failing_path:
            assert path.read_bytes() == b""
            raise sentinel

    with pytest.raises(exception_type) as failure:
        extract_audio(source, target, on_created=created)
    assert failure.value is sentinel
    assert source.read_bytes() == original and unrelated.read_bytes() == b"preserve unrelated audio"
    assert list(tmp_path.iterdir()) == [unrelated]


@pytest.mark.parametrize("phase", ["staging", "target"])
def test_cleanup_inode_guard_preserves_replacement_after_callback_failure(tmp_path, media_fixtures, monkeypatch, phase):
    audio = importlib.import_module("lumabox.audio")
    target = tmp_path / ".audio-replaced.m4a"
    replaced = audio_staging_path(target) if phase == "staging" else target
    moved_original = tmp_path / "original-inode.m4a"
    original_unlink = audio._unlink_owned
    sentinel = RuntimeError("manifest persistence failed")
    recorded_identity = []

    def created(path, identity):
        if path == replaced:
            recorded_identity.append(identity)
            raise sentinel

    def replace_before_cleanup(path, identity):
        if path == replaced:
            path.rename(moved_original)
            path.write_bytes(b"replacement belongs to another owner")
        return original_unlink(path, identity)

    monkeypatch.setattr(audio, "_unlink_owned", replace_before_cleanup)
    with pytest.raises(RuntimeError) as failure:
        extract_audio(media_fixtures[0], target, on_created=created)
    assert failure.value is sentinel
    stat = moved_original.stat()
    assert recorded_identity == [(stat.st_dev, stat.st_ino)]
    assert replaced.read_bytes() == b"replacement belongs to another owner"
    assert set(tmp_path.iterdir()) == {moved_original, replaced}


@pytest.mark.parametrize("phase", ["extract", "output_probe"])
def test_staging_hardlink_replacement_cannot_overwrite_the_source(tmp_path, media_fixtures, monkeypatch, phase):
    audio = importlib.import_module("lumabox.audio")
    source = tmp_path / "saved-source.mp4"
    original = media_fixtures[0].read_bytes()
    source.write_bytes(original)
    target = tmp_path / ".audio-raced-staging.m4a"
    staging = audio_staging_path(target)
    original_tree = audio.ProcessTree
    replacement_created = []
    attempted = []

    class ReplaceWithSource(original_tree):
        def __enter__(self):
            if ((phase == "extract" and self.command[0] == "ffmpeg" and "-c:a" in self.command) or
                    (phase == "output_probe" and self.command[0] == "ffprobe" and str(staging) in self.command)):
                attempted.append(True)
                # On Windows an open output handle may itself prevent unlinking.
                # When replacement is possible, all writes must remain on the
                # original descriptor and cleanup must preserve this new inode.
                staging.unlink()
                os.link(source, staging)
                replacement_created.append(True)
            return super().__enter__()

    monkeypatch.setattr(audio, "ProcessTree", ReplaceWithSource)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, target)
    assert failure.value.code == "AUDIO_FAILED" and attempted
    assert source.read_bytes() == original and not target.exists()
    if replacement_created:
        assert staging.read_bytes() == original
        assert set(tmp_path.iterdir()) == {source, staging}
    else:
        assert list(tmp_path.iterdir()) == [source]


def test_unavailable_fd_protocol_fails_cleanly_without_path_output(tmp_path, media_fixtures, monkeypatch):
    audio = importlib.import_module("lumabox.audio")
    original_tree = audio.ProcessTree
    attempted_outputs = []
    source = media_fixtures[0]
    original = source.read_bytes()
    target = tmp_path / "fallback.m4a"

    class UnsupportedFd(original_tree):
        def __enter__(self):
            if self.command[0] == "ffmpeg" and "-c:a" in self.command:
                attempted_outputs.append(self.command[-1])
                if self.command[-1] == "fd:":
                    # Model an older FFmpeg rejecting its unknown fd protocol.
                    self.command = [sys.executable, "-c", "import sys; sys.exit(1)"]
            return super().__enter__()

    monkeypatch.setattr(audio, "ProcessTree", UnsupportedFd)
    with pytest.raises(LumaBoxError) as failure:
        extract_audio(source, target)
    assert attempted_outputs == ["fd:"] and failure.value.code == "AUDIO_FAILED"
    assert source.read_bytes() == original and not list(tmp_path.iterdir())
