import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import lumabox.transfer as transfer
from lumabox.errors import LumaBoxError
from lumabox.models import MediaResource


@pytest.fixture
def server(monkeypatch):
    routes = {}
    cookies = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            cookies.append((self.path, self.headers.get("Cookie")))
            if callable(routes[self.path]):
                try:
                    routes[self.path](self.wfile)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            status, headers, body = routes[self.path]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                if callable(body):
                    body(self.wfile)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(transfer, "resolve_public", lambda host, **kwargs: ("93.184.216.34",))
    monkeypatch.setattr(transfer, "PinnedHTTPSConnection", lambda host, ip, timeout: http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=timeout))
    yield routes, cookies
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def download(path, limit=100):
    return transfer.download_resource(MediaResource("https://p3.douyinpic.com/start", 1), path,
        lambda url: {"Cookie": "only-p3"} if "p3.douyinpic.com" in url else {}, limit)


def test_streams_real_body_across_redirect_without_cookie_leak(server, tmp_path):
    routes, cookies = server
    routes["/start"] = (302, {"Location": "https://p4.douyinpic.com/end"}, b"")
    routes["/end"] = (200, {"Content-Type": "image/jpeg", "Content-Length": "6"}, b"abcdef")
    target = tmp_path / "out.jpg"
    assert download(target) == 6
    assert target.read_bytes() == b"abcdef"
    assert cookies == [("/start", "only-p3"), ("/end", None)]


@pytest.mark.parametrize("length", [True, False])
def test_progress_counts_actual_disk_writes_and_only_reports_known_length(server, tmp_path, length):
    payload = b"a" * 70000
    headers = {"Content-Type": "video/mp4"}
    if length:
        headers["Content-Length"] = str(len(payload))
    server[0]["/start"] = (200, headers, payload)
    target = tmp_path / "out.mp4"
    observations = []
    def progress(received, total):
        # Read the actual file at the callback boundary, not a guessed timer.
        observations.append((received, total, target.stat().st_size))
    assert transfer.download_resource(MediaResource("https://p3.douyinpic.com/start", 1), target,
        lambda url: {}, 100000, progress=progress) == 70000
    assert observations and observations[-1] == (70000, 70000 if length else None, 70000)
    assert all(received == size and received <= 70000 for received, _, size in observations)
    assert [item[0] for item in observations] == sorted(item[0] for item in observations)


@pytest.mark.parametrize("status,headers,body,code", [
    (403, {}, b"", "BLOCKED"), (401, {}, b"", "AUTH_REQUIRED"), (429, {}, b"", "RATE_LIMITED"),
    (302, {"Location": "https://127.0.0.1/private"}, b"", "UNSAFE_TARGET"),
    (200, {"Content-Type": "image/jpeg", "Content-Length": "101"}, b"x", "SIZE_LIMIT"),
    (200, {"Content-Type": "image/jpeg"}, b"x" * 101, "SIZE_LIMIT"),
    (200, {"Content-Type": "image/jpeg", "Content-Length": "20"}, b"x", "DOWNLOAD_FAILED"),
    (200, {"Content-Type": "image/jpeg"}, b"", "DOWNLOAD_FAILED"),
    (206, {"Content-Type": "video/mp4"}, b"x", "DOWNLOAD_FAILED"),
    (200, {"Content-Type": "text/html"}, b"<html>", "UNSUPPORTED_MEDIA"),
])
def test_failures_remove_only_current_partial(server, tmp_path, status, headers, body, code):
    server[0]["/start"] = (status, headers, body)
    target = tmp_path / "out.jpg"
    with pytest.raises(LumaBoxError, match=code):
        download(target)
    assert not target.exists()


def test_existing_file_is_preserved(server, tmp_path):
    server[0]["/start"] = (200, {"Content-Type": "image/jpeg"}, b"new")
    target = tmp_path / "out.jpg"
    target.write_bytes(b"previous")
    with pytest.raises(LumaBoxError):
        download(target)
    assert target.read_bytes() == b"previous"


def test_sixth_redirect_fails(server, tmp_path):
    server[0]["/start"] = (302, {"Location": "/start"}, b"")
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        download(tmp_path / "out")
    assert len(server[1]) == 6


def test_idle_timeout_removes_partial(server, tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "IDLE_TIMEOUT", 0.1)
    def slow(writer):
        time.sleep(0.3)
        writer.write(b"late")
    server[0]["/start"] = (200, {"Content-Type": "image/jpeg"}, slow)
    target = tmp_path / "out.jpg"
    with pytest.raises(LumaBoxError, match="RESOURCE_TIMEOUT") as failure:
        download(target)
    assert not target.exists()
    assert failure.value.diagnostics == {"resource_order": 1, "host": "p3.douyinpic.com",
                                         "http_status": 200, "content_type": "image/jpeg"}


def test_total_deadline_stops_trickle_download(server, tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "TOTAL_TIMEOUT", 0.15)
    def trickle(writer):
        for _ in range(8):
            writer.write(b"x")
            writer.flush()
            time.sleep(0.05)
    server[0]["/start"] = (200, {"Content-Type": "image/jpeg"}, trickle)
    target = tmp_path / "out.jpg"
    with pytest.raises(LumaBoxError, match="RESOURCE_TIMEOUT") as failure:
        download(target)
    assert not target.exists()
    assert failure.value.diagnostics == {"resource_order": 1, "host": "p3.douyinpic.com",
                                         "http_status": 200, "content_type": "image/jpeg"}


def test_close_delimited_response_resets_socket_to_remaining_deadline(server, tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "TOTAL_TIMEOUT", 0.4)
    def slow_tail(writer):
        time.sleep(0.3)
        writer.write(b"first")
        writer.flush()
        time.sleep(1)
    server[0]["/start"] = (200, {"Content-Type": "image/jpeg"}, slow_tail)
    start = time.monotonic()
    with pytest.raises(LumaBoxError, match="RESOURCE_TIMEOUT"):
        download(tmp_path / "out.jpg")
    assert time.monotonic() - start < 0.6


@pytest.mark.parametrize("phase", ["headers", "chunk_size", "trailers"])
def test_absolute_deadline_bounds_http_framing(server, tmp_path, monkeypatch, phase):
    monkeypatch.setattr(transfer, "TOTAL_TIMEOUT", 0.2)
    def trickle(writer):
        if phase == "headers":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nX-Slow: ")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nTransfer-Encoding: chunked\r\n\r\n")
            if phase == "trailers":
                writer.write(b"1\r\nx\r\n0\r\nX-Slow: ")
        for _ in range(12):
            writer.write(b"1" if phase == "chunk_size" else b"x")
            writer.flush()
            time.sleep(0.05)
        writer.write(b"\r\n\r\n")
    server[0]["/start"] = trickle
    target = tmp_path / "out.jpg"
    start = time.monotonic()
    with pytest.raises(LumaBoxError, match="RESOURCE_TIMEOUT"):
        download(target)
    assert time.monotonic() - start < 0.4
    assert not target.exists()


def test_cancel_interrupts_trickled_headers(server, tmp_path):
    def trickle(writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nX-Slow: ")
        for _ in range(12):
            writer.write(b"x")
            writer.flush()
            time.sleep(0.05)
        writer.write(b"\r\n\r\n")
    server[0]["/start"] = trickle
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set)
    timer.start()
    target = tmp_path / "out.jpg"
    start = time.monotonic()
    try:
        with pytest.raises(LumaBoxError, match="INTERRUPTED") as failure:
            transfer.download_resource(MediaResource("https://p3.douyinpic.com/start", 1), target, lambda url: {}, 100, cancel)
        assert time.monotonic() - start < 0.4
        assert not target.exists()
        assert failure.value.diagnostics == {"resource_order": 1}
    finally:
        timer.cancel()
