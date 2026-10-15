"""Start or reuse this project's local web service, then open its ready URL."""
import argparse
from contextlib import contextmanager, suppress
import http.client
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import webbrowser

from . import __version__


class LaunchError(Exception):
    """A startup failure that can be shown without exposing server output."""


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _same_directory(value, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value)
        return path.is_absolute() and path.resolve() == expected
    except (OSError, ValueError):
        return False


class _DeadlineSocket(socket.socket):
    """Keep HTTP's buffered header/body reads within one absolute deadline."""
    def __init__(self, connected: socket.socket, deadline: float):
        super().__init__(connected.family, connected.type, connected.proto,
                         fileno=connected.detach())
        self.deadline = deadline

    def _limit_wait(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local service probe deadline exceeded")
        self.settimeout(remaining)

    def recv_into(self, buffer, *args):
        # socket.makefile/SocketIO uses recv_into even for HTTP readline and
        # read(n), so bytes arriving one by one cannot reset the total budget.
        self._limit_wait()
        return super().recv_into(buffer, *args)

    def sendall(self, data, *args):
        self._limit_wait()
        return super().sendall(data, *args)


def _probe(project: Path, port: int, timeout: float, *, deadline: float | None = None) -> str:
    """Distinguish a free port from a matching service and any other listener."""
    probe_deadline = time.monotonic() + timeout
    if deadline is not None:
        probe_deadline = min(probe_deadline, deadline)
    # Windows/firewall settings can silently drop connects to an empty port.
    # An exclusive bind checks availability without relying on refusal timing.
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
            return "free"
        except OSError:
            pass
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    wrapped = response = None
    try:
        remaining = probe_deadline - time.monotonic()
        if remaining <= 0:
            return "occupied"
        connection.timeout = remaining
        connection.connect()
        wrapped = _DeadlineSocket(connection.sock, probe_deadline)
        connection.sock = wrapped
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        if response.status != 200:
            return "occupied"
        body = response.read(16385)
        if len(body) > 16384 or time.monotonic() >= probe_deadline:
            return "occupied"
        identity = json.loads(body)
        if (isinstance(identity, dict) and identity.get("application") == "lumabox"
                and identity.get("version") == __version__
                and _same_directory(identity.get("data_directory"), project / "WebData")
                and _same_directory(identity.get("output_directory"), project / "Output")):
            return "ready"
    except (OSError, http.client.HTTPException, ValueError):
        pass
    finally:
        # No reader threads are needed. Shutdown also covers a socket retained
        # by HTTPResponse's makefile after an HTTP/1.0 connection was closed.
        if wrapped is not None:
            with suppress(OSError):
                wrapped.shutdown(socket.SHUT_RDWR)
        if response is not None:
            response.close()
        connection.close()
        if wrapped is not None:
            wrapped.close()
    return "occupied"


@contextmanager
def _startup_lock(directory: Path, deadline: float):
    # A separate OS-backed lock serializes launchers without claiming the web
    # worker's instance.lock. Process exit also releases this lock after a crash.
    if directory.exists() and _is_link(directory):
        raise LaunchError("数据目录不能是链接，请检查 WebData。")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "launcher.lock"
    if path.exists() and _is_link(path):
        raise LaunchError("启动锁文件不能是链接，请检查 WebData。")
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        locked = False
        try:
            while not locked:
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LaunchError("等待其他启动过程超时，请稍后重试。") from None
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)


def _stop_child(child: subprocess.Popen):
    """Stop only the process started by this invocation after startup failure."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=1)


def launch(project_directory: Path, port: int = 8765, *, open_browser: bool = True,
           startup_timeout: float = 20) -> str:
    """Return only after this project's service is ready; preserve it on exit."""
    if (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            or not math.isfinite(startup_timeout) or startup_timeout <= 0):
        raise LaunchError("端口或启动等待时间不正确。")
    project = Path(project_directory).resolve()
    if not project.is_dir():
        raise LaunchError("项目目录不存在。")
    deadline = time.monotonic() + startup_timeout
    log_path = project / "WebData" / "server.log"
    url = f"http://127.0.0.1:{port}"
    with _startup_lock(project / "WebData", deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LaunchError("光匣启动等待超时，请稍后重试。")
        state = _probe(project, port, min(.25, remaining), deadline=deadline)
        if state == "occupied":
            raise LaunchError(f"端口 {port} 已被其他服务或旧版光匣占用，请关闭它或选择其他端口。")
        if state == "free":
            if time.monotonic() >= deadline:
                raise LaunchError("光匣启动等待超时，请稍后重试。")
            if log_path.exists() and _is_link(log_path):
                raise LaunchError("服务日志不能是链接，请检查 WebData/server.log。")
            environment = os.environ.copy()
            environment.pop("LUMABOX_WEB_PASSWORD", None)
            environment["PYTHONUTF8"] = "1"
            # The same interpreter must import the same package even when the
            # caller runs from a different directory or source is not installed.
            package_root = str(Path(__file__).resolve().parent.parent)
            inherited_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, inherited_path)))
            command = [sys.executable, "-m", "lumabox.web", "--port", str(port),
                       "--data", str(project / "WebData"), "--output", str(project / "Output")]
            with log_path.open("wb") as log:
                child = subprocess.Popen(
                    command, cwd=project, env=environment, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LaunchError(f"光匣启动超时，请查看日志：{log_path}")
                    state = _probe(project, port, min(.25, remaining), deadline=deadline)
                    if time.monotonic() >= deadline:
                        raise LaunchError(f"光匣启动超时，请查看日志：{log_path}")
                    if state == "ready":
                        break
                    if child.poll() is not None:
                        raise LaunchError(f"光匣启动失败，请检查网页依赖和端口占用；日志：{log_path}")
                    time.sleep(min(.1, max(0, deadline - time.monotonic())))
            except BaseException:
                _stop_child(child)
                raise
    if open_browser:
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error:
            raise LaunchError(f"无法打开默认浏览器，请手动访问：{url}") from None
        if not opened:
            raise LaunchError(f"无法打开默认浏览器，请手动访问：{url}")
    return url


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lumabox-start", description="启动光匣并打开本机工作台")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(), help="项目目录，默认当前目录")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不打开默认浏览器")
    parser.add_argument("--startup-timeout", type=float, default=20, help="启动等待秒数，默认 20")
    args = parser.parse_args(argv)
    try:
        url = launch(args.project_dir, args.port, open_browser=not args.no_browser,
                     startup_timeout=args.startup_timeout)
    except (LaunchError, OSError) as exc:
        print(f"无法启动光匣：{exc}", file=sys.stderr)
        return 1
    print(f"光匣已就绪：{url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
