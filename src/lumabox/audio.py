"""Extract a verified M4A attachment from an already saved local MP4."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
from collections.abc import Callable

from .errors import LumaBoxError
from .web.process import ProcessTree


TOTAL_TIMEOUT = 900.0
PROBE_LIMIT = 256 * 1024
_INPUT_OPTIONS = ["-protocol_whitelist", "file,pipe", "-f", "mov"]


def _failed():
    return LumaBoxError("AUDIO_FAILED", "The local video audio could not be extracted and verified")


def audio_staging_path(target: Path) -> Path:
    """Locate a unique attachment's staging file, including after a worker crash."""
    target = Path(target)
    return target.with_name(f".lumabox-{target.name}.partial.m4a")


def _local(path: Path) -> Path:
    path = Path(path)
    # File protocols also accept UNC paths; these attachments must stay local.
    if str(path).startswith(("\\\\", "//")) or any(":" in part for part in path.parts[1:]):
        raise _failed()
    return path


def _unlink_owned(path, identity):
    """Never remove a replacement placed at one of our output paths."""
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        path.unlink()


class _Operation:
    def __init__(self, cancelled, max_bytes, on_created):
        self.cancelled, self.max_bytes = cancelled, max_bytes
        self.on_created, self.creation_error = on_created, None
        self.deadline = time.monotonic() + TOTAL_TIMEOUT
        self.output = None
        self.output_identity = None

    def created(self, path, identity):
        if self.on_created is not None:
            try:
                self.on_created(path, identity)
            except BaseException as error:
                self.creation_error = error
                raise

    def check(self):
        if self.cancelled():
            raise LumaBoxError("INTERRUPTED", "Audio extraction cancelled")
        if time.monotonic() >= self.deadline:
            raise LumaBoxError("TIMEOUT", "Audio extraction exceeded its time limit")
        if self.output is not None:
            try:
                stat = self.output.stat(follow_symlinks=False)
            except OSError:
                raise _failed() from None
            if (stat.st_dev, stat.st_ino) != self.output_identity:
                raise _failed()
            if stat.st_size > self.max_bytes:
                raise LumaBoxError("SIZE_LIMIT", "Extracted audio exceeds the size limit")

    def run(self, command, *, capture=False, stdout=None):
        self.check()
        with tempfile.TemporaryFile() as captured:
            try:
                output = stdout if stdout is not None else captured if capture else subprocess.DEVNULL
                with ProcessTree(command, output) as tree:
                    while True:
                        self.check()
                        if capture and os.fstat(captured.fileno()).st_size > PROBE_LIMIT:
                            raise _failed()
                        code = tree.process.poll()
                        if code is not None:
                            break
                        time.sleep(.02)
            except FileNotFoundError:
                raise LumaBoxError("DEPENDENCY_MISSING", "Check the FFmpeg and FFprobe installation") from None
            self.check()
            if code != 0:
                raise _failed()
            if capture:
                captured.seek(0)
                payload = captured.read(PROBE_LIMIT + 1)
                if len(payload) > PROBE_LIMIT:
                    raise _failed()
                self.check()
                return json.loads(payload)

    def probe(self, path):
        data = self.run(["ffprobe", "-v", "error", *_INPUT_OPTIONS, "-i", str(path),
                         "-show_entries", "stream=index,codec_name,codec_type:format=format_name,duration",
                         "-of", "json"], capture=True)
        if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
            raise _failed()
        if "mp4" not in data["format"]["format_name"].split(","):
            raise _failed()
        duration = float(data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise _failed()
        return data, duration


def extract_audio(source: Path, target: Path, *, cancelled: Callable[[], bool] = lambda: False,
                  max_bytes: int = 200 * 1024 ** 2,
                  on_created: Callable[[Path, tuple[int, int]], None] | None = None) -> dict:
    """Copy AAC packets, or transcode other audio to AAC, and publish without replacing files.

    The caller retains its source-file lease throughout this synchronous operation.
    Only files created by this operation can be removed on failure.
    The optional callback records each new file's device and inode before writes;
    callback errors propagate after the operation cleans its own files.
    """
    operation = _Operation(cancelled, max_bytes, on_created)
    temporary = None
    temporary_identity = None
    target_identity = None
    published = False
    try:
        operation.check()
        source, target = _local(source), _local(target)
        if target.suffix.lower() != ".m4a" or os.path.lexists(target):
            raise _failed()
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise LumaBoxError("SIZE_LIMIT", "Extracted audio exceeds the size limit")
        source = source.resolve(strict=True)
        target = target.parent.resolve(strict=True) / target.name
        if not source.is_file():
            raise _failed()
        with source.open("rb") as video:
            signature = video.read(12)
        operation.check()
        if len(signature) < 12 or signature[4:8] != b"ftyp":
            raise _failed()
        data, _ = operation.probe(source)
        if not any(stream.get("codec_type") == "video" for stream in data["streams"]):
            raise _failed()
        audio = next((stream for stream in data["streams"] if stream.get("codec_type") == "audio"), None)
        if audio is None:
            raise LumaBoxError("NO_AUDIO", "The saved video has no audio track")
        operation.check()
        temporary = audio_staging_path(target)
        with temporary.open("xb") as reservation:
            stat = os.fstat(reservation.fileno())
            temporary_identity = (stat.st_dev, stat.st_ino)
            operation.output = temporary
            operation.output_identity = temporary_identity
            operation.created(temporary, temporary_identity)
            codec = ["-c:a", "copy"] if audio.get("codec_name") == "aac" else ["-c:a", "aac", "-b:a", "192k"]
            # fd keeps the muxer seekable without reopening a replaceable pathname.
            # The child receives only our exclusively created file as stdout.
            operation.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", *_INPUT_OPTIONS,
                           "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                           "-map_metadata", "-1", "-map_chapters", "-1", *codec,
                           "-protocol_whitelist", "fd", "-f", "ipod", "-fd", "1", "fd:"], stdout=reservation)
        output, duration = operation.probe(temporary)
        streams = output["streams"]
        if len(streams) != 1 or streams[0].get("codec_type") != "audio" or streams[0].get("codec_name") != "aac":
            raise _failed()
        operation.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", *_INPUT_OPTIONS,
                       "-i", str(temporary), "-map", "0:a:0", "-f", "null", "-"])
        digest = hashlib.sha256()
        with temporary.open("rb") as audio_file:
            while True:
                operation.check()
                chunk = audio_file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        operation.check()
        size = temporary.stat().st_size
        if size <= 0:
            raise _failed()
        result = {"name": target.name, "bytes": size, "sha256": digest.hexdigest(),
                  "width": None, "height": None, "duration": duration, "mime": "audio/mp4"}
        # Exclusive creation preserves even a target that appeared while FFmpeg
        # ran, and also works on local filesystems without hardlink support.
        with target.open("xb") as destination:
            stat = os.fstat(destination.fileno())
            target_identity = (stat.st_dev, stat.st_ino)
            operation.created(target, target_identity)
            with temporary.open("rb") as audio_file:
                while True:
                    operation.check()
                    chunk = audio_file.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
        operation.output = target
        operation.output_identity = target_identity
        operation.check()
        _unlink_owned(temporary, temporary_identity)
        temporary_identity = None
        operation.check()
        published = True
        return result
    except LumaBoxError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        if error is operation.creation_error:
            raise
        raise _failed() from None
    finally:
        cleanup_failed = False
        for path, identity in ((temporary, temporary_identity),
                               (target if target_identity is not None else None,
                                target_identity if not published else None)):
            if path is not None and identity is not None:
                try:
                    _unlink_owned(path, identity)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            raise _failed() from None
