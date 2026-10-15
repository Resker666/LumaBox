"""Launcher checks use real HTTP listeners and isolated background web services."""
import contextlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from lumabox import __version__


SOURCE = Path(__file__).resolve().parents[1] / "src"
START_SCRIPT = SOURCE.parent / "start-lumabox.cmd"


def launcher():
    assert importlib.util.find_spec("lumabox.launch") is not None, "one-click launcher is missing"
    return importlib.import_module("lumabox.launch")


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def session(project):
    return {
        "application": "lumabox", "version": __version__,
        "data_directory": str(project / "WebData"), "output_directory": str(project / "Output"),
        "csrf": "local-test-token", "video_limit_mib": 1024, "max_video_limit_mib": 8192,
        "pending_limit": 10, "persistent": True,
        "image_limit_mib": 20, "gallery_limit_mib": 200, "gallery_count_limit": 50,
    }


def read_session(port):
    with urlopen(f"http://127.0.0.1:{port}/api/session", timeout=1) as response:
        return json.load(response)


@contextlib.contextmanager
def http_service(payload):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextlib.contextmanager
def dripping_http_service(part):
    """Send often enough to defeat an idle timeout, with finite RED runs."""
    stopped, disconnected = threading.Event(), threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(.1)
    connections = []

    def serve():
        try:
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                    break
                except socket.timeout:
                    continue
            else:
                return
            connections.append(connection)
            with connection:
                connection.settimeout(2)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(1024)
                    if not chunk:
                        return
                    request += chunk
                if part == "body":
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
                else:
                    connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(20):
                    connection.sendall(b"x")
                    if stopped.wait(.1):
                        return
                    if select.select([connection], [], [], 0)[0] and connection.recv(1) == b"":
                        disconnected.set()
                        return
                if part == "headers":
                    connection.sendall(b"\r\nContent-Length: 2\r\n\r\n{}")
                if connection.recv(1) == b"":
                    disconnected.set()
        except OSError:
            disconnected.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], disconnected, thread
    finally:
        stopped.set()
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        thread.join(timeout=3)
        listener.close()


def records(project):
    marker = project / "child-starts.jsonl"
    return [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []


def stop_test_children(project):
    # Only PIDs recorded by our test hook in this temporary project are ours.
    for record in records(project):
        assert record["cwd"] == str(project)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(record["pid"], signal.SIGTERM)


def process_exited(pid, timeout=2):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return True
        try:
            return kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(.02)
    return False


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "收藏 workspace with spaces"
    directory.mkdir()
    hook_dir = tmp_path / "startup hooks"
    hook_dir.mkdir()
    marker = directory / "child-starts.jsonl"
    # Runs inside the real interpreter before real web startup. This records
    # environment inheritance and creates a deterministic readiness delay.
    (hook_dir / "sitecustomize.py").write_text(
        "import json,os,sys,time\n"
        "from pathlib import Path\n"
        "if 'lumabox.web' in sys.orig_argv:\n"
        f"    marker=Path({str(marker)!r})\n"
        "    record={'pid':os.getpid(),'cwd':os.getcwd(),'executable':sys.executable,"
        "'argv':sys.orig_argv,'password_present':'LUMABOX_WEB_PASSWORD' in os.environ}\n"
        "    with marker.open('a',encoding='utf-8') as stream: stream.write(json.dumps(record)+'\\n')\n"
        "    time.sleep(float(os.environ.get('LUMABOX_TEST_START_DELAY','0')))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(SOURCE), str(hook_dir))))
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("LUMABOX_WEB_PASSWORD", "must-never-reach-child-or-log")
    try:
        yield directory
    finally:
        stop_test_children(directory)


def launcher_process(project, port, *, cwd=None):
    log = (project / f"test-launch-{time.monotonic_ns()}.log").open("wb")
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "lumabox.launch", "--project-dir", str(project),
             "--port", str(port), "--no-browser", "--startup-timeout", "10"],
            cwd=cwd or SOURCE.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        log.close()
    return child


def test_matching_service_is_reused_and_browser_opens_ready_url(project, monkeypatch):
    # Catch accepting a service without reading its identity, or spawning again.
    module = launcher()
    opened = []
    with http_service(session(project)) as port:
        def open_browser(url):
            ready = read_session(port)
            opened.append((url, ready["application"], ready["data_directory"]))
            return True

        monkeypatch.setattr(module.webbrowser, "open", open_browser)
        assert module.launch(project, port) == f"http://127.0.0.1:{port}"
        assert opened == [(f"http://127.0.0.1:{port}", "lumabox", str(project / "WebData"))]
        assert records(project) == []


def test_browser_failure_reports_the_ready_service_url(project, monkeypatch, capsys):
    # Catch reporting a successful one-click open when no browser handled it.
    module = launcher()
    monkeypatch.setattr(module.webbrowser, "open", lambda url: False)
    with http_service(session(project)) as port:
        assert module.main(["--project-dir", str(project), "--port", str(port)]) == 1
        assert f"http://127.0.0.1:{port}" in capsys.readouterr().err
        assert read_session(port)["application"] == "lumabox"
        assert records(project) == []


@pytest.mark.parametrize("change", [
    {"application": "another-app"}, {"version": "old-version"},
    {"data_directory": "unrelated/WebData"}, {"output_directory": "unrelated/Output"},
    {"application": None}, {"data_directory": None},
])
def test_unrelated_or_old_service_is_rejected_without_browser_or_shutdown(project, monkeypatch, change):
    # Catch opening an unrelated/old app merely because its port responds.
    module = launcher()
    payload = {**session(project), **change}
    monkeypatch.setattr(module.webbrowser, "open", lambda url: pytest.fail("opened occupied port"))
    with http_service(payload) as port:
        with pytest.raises(module.LaunchError, match="占用"):
            module.launch(project, port, startup_timeout=1)
        assert read_session(port) == payload
        assert records(project) == []


def test_non_http_listener_is_rejected_with_bounded_wait(project, monkeypatch):
    # Catch hanging indefinitely on a listener which never speaks HTTP.
    module = launcher()
    monkeypatch.setattr(module.webbrowser, "open", lambda url: pytest.fail("opened occupied port"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        started = time.monotonic()
        with pytest.raises(module.LaunchError, match="占用"):
            module.launch(project, port, startup_timeout=1)
        assert time.monotonic() - started < 3
        assert records(project) == []


@pytest.mark.parametrize("part", ["body", "headers"])
def test_dripping_http_response_obeys_absolute_startup_deadline_and_closes_connection(project, part):
    # Catch resetting the timeout for every received byte in headers or body.
    module = launcher()
    with dripping_http_service(part) as (port, disconnected, server_thread):
        started = time.monotonic()
        with pytest.raises(module.LaunchError, match="占用"):
            module.launch(project, port, open_browser=False, startup_timeout=.25)
        elapsed = time.monotonic() - started
        assert elapsed < .75, f"{part} probe took {elapsed:.2f}s for a .25s deadline"
        assert disconnected.wait(.6), "timed-out probe left its socket open"
        server_thread.join(timeout=.6)
        assert not server_thread.is_alive(), "timed-out probe left a dangling read"
        assert records(project) == []


def test_real_service_is_ready_before_browser_opens_and_credentials_are_not_inherited(project, monkeypatch):
    # Catch premature browser opening, shell/string command paths, or leaking the old password.
    module = launcher()
    port = unused_port()
    monkeypatch.setenv("LUMABOX_TEST_START_DELAY", "0.4")
    opened = []

    def open_browser(url):
        opened.append((url, read_session(port)))
        return True

    monkeypatch.setattr(module.webbrowser, "open", open_browser)
    assert module.launch(project, port, startup_timeout=10) == f"http://127.0.0.1:{port}"
    assert opened[0][1]["data_directory"] == str(project / "WebData")
    assert opened[0][1]["output_directory"] == str(project / "Output")
    children = records(project)
    assert len(children) == 1
    assert Path(children[0]["executable"]).resolve() == Path(sys.executable).resolve()
    assert not children[0]["password_present"]
    log = project / "WebData" / "server.log"
    assert log.exists()
    assert b"must-never-reach-child-or-log" not in log.read_bytes()


def test_concurrent_and_repeated_launches_keep_one_background_service(project, monkeypatch):
    # Catch releasing the startup lock before readiness or duplicating servers.
    launcher()
    monkeypatch.setenv("LUMABOX_TEST_START_DELAY", "0.5")
    port = unused_port()
    processes = [launcher_process(project, port) for _ in range(3)]
    try:
        assert [process.wait(timeout=15) for process in processes] == [0, 0, 0]
        assert read_session(port)["data_directory"] == str(project / "WebData")
        repeated = launcher_process(project, port)
        assert repeated.wait(timeout=15) == 0
        assert read_session(port)["application"] == "lumabox"
        assert len(records(project)) == 1
        assert sorted(path.name for path in (project / "WebData").glob("*.log")) == ["server.log"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


def test_startup_timeout_stops_only_the_child_it_started(project, monkeypatch):
    # Catch a service left behind after timeout, and unbounded startup waits.
    module = launcher()
    port = unused_port()
    monkeypatch.setenv("LUMABOX_TEST_START_DELAY", "5")
    started = time.monotonic()
    with pytest.raises(module.LaunchError, match="超时"):
        module.launch(project, port, open_browser=False, startup_timeout=.5)
    assert time.monotonic() - started < 3
    assert len(records(project)) == 1
    assert process_exited(records(project)[0]["pid"]), "actual Python child survived startup timeout"
    with socket.socket() as client:
        assert client.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.skipif(os.name != "nt", reason="double-click launcher is a Windows command file")
def test_command_file_uses_its_own_directory_with_spaces(project, tmp_path, monkeypatch):
    # Catch unquoted command paths or deriving data/output from the caller's cwd.
    launcher()
    assert START_SCRIPT.exists(), "double-click command file is missing"
    shutil.copyfile(START_SCRIPT, project / START_SCRIPT.name)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    port = unused_port()
    output = project / "test-command.log"
    with output.open("wb") as log:
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
             str(project / START_SCRIPT.name), "--port", str(port), "--no-browser", "--startup-timeout", "10"],
            cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    assert result.returncode == 0, output.read_text(encoding="utf-8", errors="replace")
    assert read_session(port)["data_directory"] == str(project / "WebData")
    assert read_session(port)["output_directory"] == str(project / "Output")
    assert len(records(project)) == 1


@pytest.mark.parametrize("port,timeout", [(0, 1), (65536, 1), (8766, 0)])
def test_invalid_settings_do_not_start_a_child(project, port, timeout):
    # Catch invalid ports/timeouts escaping as socket errors or starting a server.
    module = launcher()
    with pytest.raises(module.LaunchError):
        module.launch(project, port, open_browser=False, startup_timeout=timeout)
    assert records(project) == []
