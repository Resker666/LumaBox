"""Save the confirmed gallery background player's original MP3 bytes.

The parent owns an exclusive local output and the entire browser process tree.
Only bounded status records, never source URLs or credentials, cross stderr.
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit

from .audio import _Operation, _local, _unlink_owned
from .browser import BrowserSession
from .diagnostics import public_diagnostics, network_reason
from .errors import LumaBoxError, check_status
from .net import PinnedHTTPSConnection, resolve_public
from .transfer import _DownloadGuard
from .urls import parse_input, validate_audio_url
from .web.process import ProcessTree


STATUS_LIMIT = 256 * 1024
STATUS_MARKER = b"LUMABOX_GALLERY_MUSIC "
_INPUT_OPTIONS = ["-protocol_whitelist", "file,pipe", "-f", "mp3"]
_ERROR_CODES = frozenset({"NO_AUDIO", "AUDIO_FAILED", "INTERRUPTED", "SIZE_LIMIT", "DEPENDENCY_MISSING", "TIMEOUT",
    "VERIFY_FAILED", "STORAGE_FULL", "UNSAFE_TARGET", "AUTH_REQUIRED", "CHALLENGE_REQUIRED", "BLOCKED", "RATE_LIMITED",
    "NOT_FOUND", "DOWNLOAD_FAILED", "UNSUPPORTED_MEDIA"})
TRANSFER_TIMEOUT = 180.0


def _failed(code="AUDIO_FAILED"):
    return LumaBoxError(code, "The gallery background music could not be saved and verified")


def _child_result(operation, work_id, headless, output):
    command = [sys.executable, "-m", "lumabox.gallery_music", work_id, str(operation.max_bytes)]
    if headless:
        command.append("--headless")
    with tempfile.TemporaryFile() as status:
        try:
            with ProcessTree(command, output, stderr=status) as tree:
                while True:
                    operation.check()
                    if os.fstat(status.fileno()).st_size > STATUS_LIMIT:
                        raise _failed()
                    code = tree.process.poll()
                    if code is not None:
                        break
                    time.sleep(.02)
        except FileNotFoundError:
            raise LumaBoxError("DEPENDENCY_MISSING", "The browser worker could not start") from None
        operation.check()
        status.seek(0)
        payload = status.read(STATUS_LIMIT + 1)
        if len(payload) > STATUS_LIMIT:
            raise _failed()
    # Browser shutdown can emit incidental stderr. Parse only our final marker;
    # no raw child output is included in exceptions or public diagnostics.
    records = [line[len(STATUS_MARKER):] for line in payload.splitlines() if line.startswith(STATUS_MARKER)]
    if not records:
        raise _failed()
    data = json.loads(records[-1])
    if not isinstance(data, dict):
        raise _failed()
    if data.get("ok") is False:
        error_code = data.get("code")
        error_code = error_code if isinstance(error_code, str) and error_code in _ERROR_CODES else "AUDIO_FAILED"
        raise LumaBoxError(error_code, "The gallery background music could not be saved and verified",
                           diagnostics=public_diagnostics(data.get("diagnostics")))
    duration = data.get("duration")
    if code != 0 or data.get("ok") is not True or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise _failed()
    return float(duration)


def _verify(operation, path, expected_duration):
    try:
        data = operation.run(["ffprobe", "-v", "error", *_INPUT_OPTIONS, "-i", str(path),
            "-show_entries", "stream=codec_name,codec_type:format=format_name,duration", "-of", "json"], capture=True)
        streams = data["streams"]
        duration = float(data["format"]["duration"])
        if data["format"]["format_name"] != "mp3" or len(streams) != 1 or streams[0].get("codec_type") != "audio" or streams[0].get("codec_name") != "mp3":
            raise _failed("VERIFY_FAILED")
        if not math.isfinite(duration) or duration <= 0 or abs(duration - expected_duration) > .5:
            raise _failed("VERIFY_FAILED")
        decoded_duration = _decoded_duration(operation, path)
        if abs(decoded_duration - expected_duration) > .5 or abs(decoded_duration - duration) > .5:
            raise _failed("VERIFY_FAILED")
        return duration
    except LumaBoxError as exc:
        if exc.code == "AUDIO_FAILED":
            raise _failed("VERIFY_FAILED") from None
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _failed("VERIFY_FAILED") from None


def _decoded_duration(operation, path):
    # An MP3's Xing/Info header can retain its original duration after frames
    # were removed. Successful decoder exit alone does not prove completeness.
    command = ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-nostats", "-stats_period", "5",
        "-progress", "pipe:1", *_INPUT_OPTIONS, "-i", str(path), "-map", "0:a:0", "-f", "null", "-"]
    with tempfile.TemporaryFile() as progress:
        operation.check()
        try:
            with ProcessTree(command, progress) as tree:
                while True:
                    operation.check()
                    if os.fstat(progress.fileno()).st_size > STATUS_LIMIT:
                        raise _failed("VERIFY_FAILED")
                    code = tree.process.poll()
                    if code is not None:
                        break
                    time.sleep(.02)
        except FileNotFoundError:
            raise LumaBoxError("DEPENDENCY_MISSING", "Check the FFmpeg and FFprobe installation") from None
        operation.check()
        progress.seek(0)
        payload = progress.read(STATUS_LIMIT + 1)
    if code != 0 or len(payload) > STATUS_LIMIT:
        raise _failed("VERIFY_FAILED")
    fields = dict(line.split(b"=", 1) for line in payload.splitlines() if b"=" in line)
    micros = fields.get(b"out_time_us", b"")
    if fields.get(b"progress") != b"end" or not re.fullmatch(rb"[0-9]{1,15}", micros) or int(micros) <= 0:
        raise _failed("VERIFY_FAILED")
    return int(micros) / 1_000_000


def _stream_music(url, output, headers, max_bytes):
    """Stream a whole original response, validating every redirect and pinned IP."""
    guard = _DownloadGuard(time.monotonic() + TRANSFER_TIMEOUT, None)
    diagnostics = {"stage": "media_transfer"}
    try:
        for hop in range(6):
            guard.remaining()
            validate_audio_url(url, redirect=hop > 0)
            parts = urlsplit(url)
            diagnostics["host"] = parts.hostname
            addresses = resolve_public(parts.hostname, timeout=guard.remaining())
            conn = PinnedHTTPSConnection(parts.hostname, addresses[0], guard.remaining())
            guard.set_connection(conn)
            try:
                conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""), headers=headers)
                guard.capture_socket()
                if conn.sock is not None:
                    conn.sock.settimeout(guard.remaining())
                response = conn.getresponse()
                guard.remaining()
                check_status(response.status)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise LumaBoxError("UNSAFE_TARGET", "Audio redirect has no location",
                                           diagnostics={"policy_reason": "AUDIO_URL_INVALID"})
                    if hop == 5:
                        raise LumaBoxError("UNSAFE_TARGET", "Audio redirects exceeded the limit",
                                           diagnostics={"policy_reason": "AUDIO_REDIRECT_LIMIT"})
                    url = urljoin(url, location)
                    validate_audio_url(url, redirect=True)
                    continue
                if response.status != 200 or response.getheader("Content-Range"):
                    raise LumaBoxError("DOWNLOAD_FAILED", "A complete audio response was not returned")
                mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                encoding = response.getheader("Content-Encoding", "identity").strip().lower()
                if mime not in {"audio/mpeg", "application/octet-stream", "binary/octet-stream"} or encoding not in {"", "identity"}:
                    raise LumaBoxError("UNSUPPORTED_MEDIA", "The response is not original MP3 audio")
                length = response.getheader("Content-Length")
                if length is not None and not re.fullmatch(r"[0-9]{1,20}", length):
                    raise LumaBoxError("DOWNLOAD_FAILED", "Invalid audio length")
                expected = int(length) if length is not None else None
                if expected is not None and expected > max_bytes:
                    raise LumaBoxError("SIZE_LIMIT", "Gallery music exceeds the size limit")
                total = 0
                while True:
                    timeout = guard.remaining()
                    sock = conn.sock or getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(timeout)
                    block = response.read1(min(65536, max_bytes - total + 1))
                    guard.remaining()
                    if not block:
                        break
                    if total + len(block) > max_bytes:
                        raise LumaBoxError("SIZE_LIMIT", "Gallery music exceeds the size limit")
                    written = output.write(block)
                    if written != len(block):
                        raise LumaBoxError("DOWNLOAD_FAILED", "The audio response could not be saved completely")
                    total += written
                output.flush()
                if total <= 0 or (expected is not None and total != expected):
                    raise LumaBoxError("DOWNLOAD_FAILED", "The audio response was empty or incomplete")
                return total
            finally:
                conn.close()
        raise LumaBoxError("UNSAFE_TARGET", "Audio redirects exceeded the limit",
                           diagnostics={"policy_reason": "AUDIO_REDIRECT_LIMIT"})
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        try:
            guard.remaining()
        except LumaBoxError as stopped:
            exc = stopped
        if isinstance(exc, LumaBoxError):
            code = "TIMEOUT" if exc.code == "RESOURCE_TIMEOUT" else exc.code
            raise LumaBoxError(code, "The gallery music response could not be downloaded",
                diagnostics=public_diagnostics({**diagnostics, **exc.diagnostics})) from None
        reason = network_reason(exc)
        if reason:
            diagnostics["network_reason"] = reason
        code = "TIMEOUT" if isinstance(exc, (socket.timeout, TimeoutError)) else "STORAGE_FULL" if isinstance(exc, OSError) and exc.errno == 28 else "DOWNLOAD_FAILED"
        raise LumaBoxError(code, "The gallery music response could not be downloaded",
                           diagnostics=public_diagnostics(diagnostics)) from None
    finally:
        guard.close()


async def _browser_music(work_id, headless):
    link = parse_input(f"https://www.douyin.com/note/{work_id}")
    async with BrowserSession(headless=headless) as session:
        url, duration = await session.load_gallery_music(link)
        # This verified audio host does not require credentials. Session cookies
        # and signed source URLs remain confined to the short-lived child.
        headers = {"User-Agent": await session.page.evaluate("navigator.userAgent"),
                   "Referer": link.url, "Accept-Encoding": "identity"}
        return url, duration, headers


def _main():
    try:
        args = sys.argv[1:]
        if len(args) not in {2, 3} or not re.fullmatch(r"\d{15,20}", args[0]) or (len(args) == 3 and args[2] != "--headless"):
            raise _failed()
        maximum = int(args[1])
        if maximum <= 0:
            raise LumaBoxError("SIZE_LIMIT", "Gallery music exceeds the size limit")
        if os.name == "nt":
            import msvcrt
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        url, duration, headers = asyncio.run(_browser_music(args[0], len(args) == 3))
        _stream_music(url, sys.stdout.buffer, headers, maximum)
        result = {"ok": True, "duration": duration}
        code = 0
    except BaseException as exc:
        error = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else exc.code if isinstance(exc, LumaBoxError) else "AUDIO_FAILED"
        error = "TIMEOUT" if error == "RESOURCE_TIMEOUT" else error
        result = {"ok": False, "code": error if error in _ERROR_CODES else "AUDIO_FAILED"}
        if isinstance(exc, LumaBoxError):
            result["diagnostics"] = public_diagnostics(exc.diagnostics)
        code = 1
    sys.stderr.write("\n" + STATUS_MARKER.decode("ascii") + json.dumps(result, allow_nan=False) + "\n")
    sys.stderr.flush()
    return code


def download_gallery_music(work_id: str, target: Path, *, headless=False,
                           cancelled=lambda: False, max_bytes=200 * 1024 ** 2,
                           on_created=None) -> dict:
    operation = _Operation(cancelled, max_bytes, on_created)
    identity = None
    completed = False
    try:
        operation.check()
        if not isinstance(work_id, str) or not re.fullmatch(r"\d{15,20}", work_id):
            raise _failed()
        if type(max_bytes) is not int or max_bytes <= 0:
            raise LumaBoxError("SIZE_LIMIT", "Gallery music exceeds the size limit")
        target = _local(Path(target))
        if target.suffix.lower() != ".mp3" or os.path.lexists(target):
            raise _failed()
        target = target.parent.resolve(strict=True) / target.name
        with target.open("xb") as output:
            stat = os.fstat(output.fileno())
            identity = (stat.st_dev, stat.st_ino)
            operation.output, operation.output_identity = target, identity
            operation.created(target, identity)
            operation.check()
            expected_duration = _child_result(operation, work_id, headless, output)
        operation.check()
        duration = _verify(operation, target, expected_duration)
        digest = hashlib.sha256()
        with target.open("rb") as audio:
            while True:
                operation.check()
                block = audio.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        operation.check()
        size = target.stat().st_size
        if size <= 0:
            raise _failed("VERIFY_FAILED")
        result = {"name": target.name, "bytes": size, "sha256": digest.hexdigest(), "width": None,
                  "height": None, "duration": duration, "mime": "audio/mpeg"}
        completed = True
        return result
    except LumaBoxError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, subprocess.SubprocessError) as exc:
        if exc is operation.creation_error:
            raise
        if isinstance(exc, OSError) and exc.errno == 28:
            raise LumaBoxError("STORAGE_FULL", "There is not enough space to save gallery music") from None
        raise _failed() from None
    finally:
        if identity is not None and not completed:
            try:
                _unlink_owned(target, identity)
            except OSError:
                raise _failed() from None


if __name__ == "__main__":
    raise SystemExit(_main())
