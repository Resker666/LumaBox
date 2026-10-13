"""Bounded streaming downloads of resources observed in the browser."""

import http.client
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .errors import LumaBoxError, check_status
from .net import PinnedHTTPSConnection, resolve_public, bounded_call
from .urls import validate_media_url

IDLE_TIMEOUT = 15.0
TOTAL_TIMEOUT = 180.0


class _DownloadGuard:
    """Interrupt blocked HTTP headers/chunk framing as well as body reads."""

    def __init__(self, deadline, cancel):
        self.deadline, self.cancel = deadline, cancel
        self.conn = self.sock = None
        self.code = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def remaining(self):
        code = self.code
        if not code and self.cancel is not None and self.cancel.is_set():
            code = "INTERRUPTED"
        left = self.deadline - time.monotonic()
        if not code and left <= 0:
            code = "RESOURCE_TIMEOUT"
        if code:
            raise LumaBoxError(code, "Media operation stopped before completion")
        return min(IDLE_TIMEOUT, left)

    def set_connection(self, conn):
        with self.lock:
            self.conn, self.sock = conn, None

    def capture_socket(self):
        with self.lock:
            self.sock = self.conn.sock

    def _watch(self):
        while not self.stop.wait(max(0, min(0.05, self.deadline - time.monotonic()))):
            try:
                self.remaining()
            except LumaBoxError as exc:
                with self.lock:
                    self.code = exc.code
                    sock = (self.conn.sock if self.conn else None) or self.sock
                    if sock is not None:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                return

    def close(self):
        self.stop.set()
        self.thread.join()


def download_resource(resource, path: Path, headers_for_url, max_bytes: int, cancel=None) -> int:
    deadline = time.monotonic() + TOTAL_TIMEOUT
    guard = _DownloadGuard(deadline, cancel)
    url = resource.url
    created = False
    remaining = guard.remaining

    try:
        for hop in range(6):
            remaining()
            validate_media_url(url)
            parts = urlsplit(url)
            ips = resolve_public(parts.hostname, timeout=remaining(), cancel=cancel)
            headers = bounded_call(lambda: headers_for_url(url), remaining(), cancel)
            conn = PinnedHTTPSConnection(parts.hostname, ips[0], remaining())
            guard.set_connection(conn)
            try:
                remaining()
                conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""), headers=headers)
                guard.capture_socket()
                conn.sock.settimeout(remaining())
                response = conn.getresponse()
                remaining()
                check_status(response.status)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or hop == 5:
                        raise LumaBoxError("UNSAFE_TARGET", "Media redirects exceeded the limit")
                    url = urljoin(url, location)
                    validate_media_url(url)
                    continue
                if response.status != 200:
                    raise LumaBoxError("DOWNLOAD_FAILED", "A complete media response was not returned")
                mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                expected = "video/mp4" if path.suffix.lower() == ".mp4" else "image/jpeg"
                if mime not in (expected, "application/octet-stream", "binary/octet-stream"):
                    raise LumaBoxError("UNSUPPORTED_MEDIA", "The response is not a supported media type")
                length = response.getheader("Content-Length")
                if length is not None and not length.isdecimal():
                    raise LumaBoxError("DOWNLOAD_FAILED", "Invalid media length")
                expected_length = int(length) if length is not None else None
                if expected_length is not None and expected_length > max_bytes:
                    raise LumaBoxError("SIZE_LIMIT", "Media exceeds the file size limit")
                total = 0
                with path.open("xb") as output:
                    created = True
                    while True:
                        socket_timeout = remaining()
                        sock = conn.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
                        if sock is not None:
                            sock.settimeout(socket_timeout)
                        # read1 returns available bytes, preventing trickle traffic from
                        # hiding inside one long read beyond the total deadline.
                        block = response.read1(min(65536, max_bytes - total + 1))
                        remaining()
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise LumaBoxError("SIZE_LIMIT", "Media exceeds the file size limit")
                        output.write(block)
                if total == 0 or (expected_length is not None and total != expected_length):
                    raise LumaBoxError("DOWNLOAD_FAILED", "The media response was empty or incomplete")
                return total
            finally:
                conn.close()
        raise LumaBoxError("UNSAFE_TARGET", "Too many media redirects")
    except BaseException as exc:
        if created:
            path.unlink(missing_ok=True)
        if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
            remaining()
        if isinstance(exc, (LumaBoxError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, (socket.timeout, TimeoutError)):
            raise LumaBoxError("RESOURCE_TIMEOUT", "Media connection timed out") from None
        raise LumaBoxError("DOWNLOAD_FAILED", "Media could not be saved") from None
    finally:
        guard.close()
