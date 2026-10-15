"""Cancellation state races and network reasons at real user-facing boundaries."""
import json
import asyncio
import socket
import ssl
import threading
import time

import pytest
from fastapi.testclient import TestClient

from lumabox.errors import LumaBoxError
from lumabox.net import resolve_public
from lumabox.models import MediaResource
from lumabox.web.app import create_app
from lumabox.web.config import WebConfig, WebError
from lumabox.web.store import JobStore
from test_daily_api import completed, headers, ORIGIN
from web_helpers import CHILD, FixtureWorker, URL, WORK_ID, fixture_worker


@pytest.fixture
def client_store(tmp_path):
    app = create_app(WebConfig(tmp_path / "data"))
    client = TestClient(app, base_url=ORIGIN)
    try:
        yield client, app.state.store
    finally:
        client.close()


def test_pending_cancel_never_claimed_and_can_retry_or_remove(client_store):
    client, store = client_store
    first = store.create(URL)
    following = store.create(URL)
    response = client.post(f"/api/jobs/{first['id']}/cancel", json={}, headers=headers(client))
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert store.claim()["id"] == following["id"]
    assert client.get("/api/jobs", params={"status": "cancelled"}).json()["total"] == 1
    retry = client.post(f"/api/jobs/{first['id']}/retry", json={}, headers=headers(client))
    assert retry.status_code == 202 and retry.json()["status"] == "pending"
    assert client.post(f"/api/jobs/{first['id']}/remove", json={}, headers=headers(client)).status_code == 200


def test_running_cancel_waits_for_cleanup_and_wins_publication_race(client_store):
    client, store = client_store
    job = store.create(URL)
    store.claim()
    store.create(URL)
    response = client.post(f"/api/jobs/{job['id']}/cancel", json={}, headers=headers(client))
    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    assert store.claim() is None  # Never overlap with the old process.
    assert client.post(f"/api/jobs/{job['id']}/remove", json={}, headers=headers(client)).status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/cancel", json={}, headers=headers(client)).json()["status"] == "cancelling"
    with pytest.raises(WebError):
        store.finish(job["id"], {"work_id": WORK_ID, "kind": "video"}, [])
    store.fail(job["id"], "VERIFY_FAILED")  # A simultaneous process result cannot undo cancellation.
    final = store.get(job["id"])
    assert (final["status"], final["error"], final["files"]) == ("cancelled", None, [])
    assert client.post(f"/api/jobs/{job['id']}/cancel", json={}, headers=headers(client)).json()["status"] == "cancelled"
    assert store.claim() is not None


def test_completed_cancel_is_rejected_and_preserves_file(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "data")
    app = create_app(config)
    job, path, original = completed(app.state.store, media_fixtures[0])
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(f"/api/jobs/{job['id']}/cancel", json={}, headers=headers(client))
        assert response.status_code == 409
        assert response.json()["error"] == "NOT_CANCELLABLE"
        assert app.state.store.get(job["id"])["status"] == "success"
        assert path.read_bytes() == original


def test_cancel_requires_same_origin_csrf_and_empty_body(client_store):
    client, store = client_store
    job = store.create(URL)
    url = f"/api/jobs/{job['id']}/cancel"
    assert client.post(url, json={}).status_code == 403
    assert client.post(url, json={}, headers={**headers(client), "Origin": "https://evil.test"}).status_code == 403
    assert client.post(url, json={"url": "https://evil.test"}, headers=headers(client)).status_code == 400
    assert store.get(job["id"])["status"] == "pending"


def test_restart_finishes_requested_cancel_and_removes_partial(tmp_path):
    config = WebConfig(tmp_path / "data")
    store = JobStore(config)
    job = store.create(URL)
    store.claim()
    directory = config.output_dir / job["id"]
    directory.mkdir()
    (directory / "partial.mp4").write_bytes(b"incomplete")
    store.cancel(job["id"])
    reopened = JobStore(config)
    reopened.recover()
    assert reopened.get(job["id"])["status"] == "cancelled"
    assert not directory.exists()


def test_cancel_stops_real_child_cleans_only_its_files_then_runs_next(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "data")
    store = JobStore(config)
    keep = config.output_dir / "keep.mp4"
    keep.write_bytes(b"existing user media")
    source = CHILD.replace("time.sleep(3)", "(root/'partial.mp4').write_bytes(b'incomplete')\n    time.sleep(60)")
    script = tmp_path / "cancel_child.py"
    script.write_text(source, encoding="utf-8")
    worker = FixtureWorker(config, store, script, media_fixtures[0], "delayed")
    first, second = store.create(URL), store.create(URL)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    try:
        partial = config.output_dir / first["id"] / "partial.mp4"
        deadline = time.monotonic() + 8
        while not partial.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert partial.exists(), "child never entered its download"
        store.cancel(first["id"])
        thread.join(10)
        assert not thread.is_alive(), "cancel left a child running"
        assert store.get(first["id"])["status"] == "cancelled"
        assert not partial.parent.exists()
        assert keep.read_bytes() == b"existing user media"
        worker.mode = "success"
        assert worker.run_once()
        assert store.get(second["id"])["status"] == "success"
    finally:
        worker.stopping.set()
        thread.join(12)


@pytest.mark.parametrize("result,reason", [
    ("198.18.0.2", "DNS_NON_PUBLIC"), ("192.168.1.2", "DNS_NON_PUBLIC"),
    (socket.gaierror(-2, "private resolver message"), "DNS_LOOKUP_FAILED"),
])
def test_dns_failure_retains_a_safe_machine_reason(monkeypatch, result, reason):
    def lookup(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (result, 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with pytest.raises(LumaBoxError) as failure:
        resolve_public("www.douyin.com")
    assert failure.value.diagnostics["network_reason"] == reason
    assert failure.value.diagnostics["stage"] == "dns_resolution"
    assert "private resolver message" not in json.dumps(failure.value.diagnostics)


@pytest.mark.parametrize("error,reason", [
    (ConnectionRefusedError(10061, "secret"), "CONNECTION_REFUSED"),
    (ConnectionResetError(10054, "secret"), "CONNECTION_RESET"),
    (TimeoutError("secret"), "CONNECTION_TIMEOUT"),
    (ssl.SSLError("secret"), "TLS_ERROR"),
])
def test_media_connection_failure_reaches_report_safely(tmp_path, monkeypatch, error, reason):
    import lumabox.transfer as transfer
    monkeypatch.setattr(transfer, "resolve_public", lambda *a, **k: ("93.184.216.34",))
    class BrokenConnection:
        sock = None
        def request(self, *args, **kwargs):
            raise error  # Only replace the external TLS connection.
        def close(self):
            pass
    monkeypatch.setattr(transfer, "PinnedHTTPSConnection", lambda *a, **k: BrokenConnection())
    with pytest.raises(LumaBoxError) as failure:
        transfer.download_resource(MediaResource("https://v1.douyinvod.com/private?token=secret", 1),
                                   tmp_path / "video.mp4", lambda _: {}, 100)
    assert failure.value.diagnostics["network_reason"] == reason
    assert failure.value.diagnostics["host"] == "v1.douyinvod.com"
    assert "secret" not in json.dumps(failure.value.diagnostics)


def test_diagnostics_survive_real_child_without_raw_errors_or_urls(tmp_path, media_fixtures):
    config = WebConfig(tmp_path / "data")
    store = JobStore(config)
    job = store.create(URL)
    source = '''import json,sys
print(json.dumps({'status':'failed','error':'UNSAFE_TARGET','diagnostics':{
 'stage':'dns_resolution','network_reason':'DNS_NON_PUBLIC','host':'www.douyin.com',
 'reason':'secret exception','url':'https://www.douyin.com/?cookie=secret'}}))
sys.exit(1)
'''
    script = tmp_path / "dns_child.py"
    script.write_text(source, encoding="utf-8")
    FixtureWorker(config, store, script, media_fixtures[0]).run_once()
    detail = store.get(job["id"])["error_detail"]
    assert detail == {"stage": "dns_resolution", "network_reason": "DNS_NON_PUBLIC", "host": "www.douyin.com"}
    assert "secret" not in json.dumps(store.get(job["id"]))


def test_unknown_network_details_and_host_injection_are_discarded(tmp_path):
    store = JobStore(WebConfig(tmp_path / "data"))
    job = store.create(URL)
    store.fail(job["id"], "UNSAFE_TARGET", {"diagnostics": {
        "stage": "https://secret", "network_reason": "secret", "host": "www.douyin.com/?cookie=secret"}})
    assert store.get(job["id"])["error_detail"] == {}


def test_dns_timeout_is_identified_separately(monkeypatch):
    def slow_lookup(*args, **kwargs):
        time.sleep(.15)
        return []
    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    with pytest.raises(LumaBoxError) as failure:
        resolve_public("www.douyin.com", timeout=.02)
    assert failure.value.diagnostics["network_reason"] == "DNS_TIMEOUT"


async def test_browser_proxy_failure_keeps_the_connection_reason(monkeypatch, tmp_path):
    import lumabox.browser as browser_module
    import lumabox.proxy as proxy_module
    from lumabox.runner import run_download
    monkeypatch.setattr(browser_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(proxy_module, "resolve_public", lambda host: ("93.184.216.34",))
    async def refused(ip):
        raise ConnectionRefusedError(10061, "secret network detail")
    monkeypatch.setattr(proxy_module, "_dial", refused)
    result = await run_download(URL, tmp_path, True)
    assert result["error"] == "DOWNLOAD_FAILED"
    assert result["diagnostics"]["network_reason"] == "CONNECTION_REFUSED"
    assert "secret" not in json.dumps(result)


def test_cancelling_stays_visible_until_cleanup_attempt_finishes(tmp_path, media_fixtures):
    entered, release = threading.Event(), threading.Event()
    class SlowCleanupStore(JobStore):
        def try_cleanup(self, job_id):
            entered.set()
            assert release.wait(8)
            return super().try_cleanup(job_id)
    config = WebConfig(tmp_path / "data")
    store = SlowCleanupStore(config)
    job = store.create(URL)
    worker = fixture_worker(config, store, tmp_path, media_fixtures[0], "timeout")
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while store.get(job["id"])["status"] != "running" and time.monotonic() < deadline:
            time.sleep(.02)
        store.cancel(job["id"])
        assert entered.wait(8)
        assert store.get(job["id"])["status"] == "cancelling"
        assert store.claim() is None
    finally:
        release.set()
        worker.stopping.set()
        thread.join(12)
    assert store.get(job["id"])["status"] == "cancelled"


async def proxy_exchange(proxy):
    from urllib.parse import urlsplit
    address = urlsplit(proxy.server_url)
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    writer.write(b"CONNECT www.douyin.com:443 HTTP/1.1\r\n\r\n")
    await writer.drain()
    return reader, writer


async def test_established_browser_tunnel_reset_keeps_its_reason(monkeypatch):
    import lumabox.proxy as proxy_module
    from lumabox.proxy import PublicProxy
    from lumabox.browser import BrowserSession
    async def receive(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    class ResetReader:
        async def read(self, size):
            raise ConnectionResetError(10054, "private connection detail")
    async def dial(ip):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        return ResetReader(), writer
    monkeypatch.setattr(proxy_module, "resolve_public", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(proxy_module, "_dial", dial)
    try:
        async with PublicProxy() as proxy:
            reader, writer = await proxy_exchange(proxy)
            try:
                assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
                assert await asyncio.wait_for(reader.read(), 2) == b""
                session = BrowserSession()
                session.proxy = proxy
                with pytest.raises(LumaBoxError) as failure:
                    session._check_errors("www.douyin.com", navigation_failed=True)
                assert failure.value.diagnostics["network_reason"] == "CONNECTION_RESET"
                assert "private" not in json.dumps(failure.value.diagnostics)
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_browser_proxy_dns_outer_timeout_is_dns_not_connect(monkeypatch):
    import lumabox.proxy as proxy_module
    from lumabox.proxy import PublicProxy
    from lumabox.browser import BrowserSession
    original_wait = asyncio.wait_for
    async def fast_wait(awaitable, timeout):
        return await original_wait(awaitable, .03 if timeout == 15 else timeout)
    def slow_lookup(host):
        time.sleep(.1)
        raise LumaBoxError("RESOURCE_TIMEOUT", diagnostics={"network_reason": "DNS_TIMEOUT", "stage": "dns_resolution"})
    monkeypatch.setattr(proxy_module, "resolve_public", slow_lookup)
    monkeypatch.setattr(proxy_module.asyncio, "wait_for", fast_wait)
    async with PublicProxy() as proxy:
        reader, writer = await proxy_exchange(proxy)
        try:
            assert (await reader.readline()).startswith(b"HTTP/1.1 502")
            session = BrowserSession()
            session.proxy = proxy
            with pytest.raises(LumaBoxError) as failure:
                session._check_errors("www.douyin.com", navigation_failed=True)
            assert failure.value.diagnostics["network_reason"] == "DNS_TIMEOUT"
            assert failure.value.diagnostics["stage"] == "dns_resolution"
        finally:
            writer.close()
            await writer.wait_closed()
