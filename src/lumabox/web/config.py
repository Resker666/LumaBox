import os
import stat
from dataclasses import dataclass
from pathlib import Path
from ..limits import DEFAULT_VIDEO_LIMIT_MIB, video_limit_bytes
from ..errors import LumaBoxError


class WebError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


@dataclass(frozen=True)
class WebConfig:
    data_dir: Path
    port: int = 8765
    headless: bool = False
    task_timeout: float = 3600
    capacity_bytes: int | None = None
    reserve_bytes: int = 512 * 1024**2
    retention_seconds: int | None = None
    output: Path | None = None
    video_limit_mib: int = DEFAULT_VIDEO_LIMIT_MIB
    remote_mode: bool = False

    def __post_init__(self):
        if not 1 <= self.port <= 65535 or self.task_timeout <= 0:
            raise WebError("INVALID_CONFIG")
        original = Path(self.data_dir).absolute()
        if original.exists() and is_link(original):
            raise WebError("INVALID_CONFIG")
        object.__setattr__(self, "data_dir", original.resolve())
        output = Path(self.output).absolute() if self.output is not None else self.data_dir.parent / "Output"
        if output.exists() and is_link(output):
            raise WebError("INVALID_CONFIG")
        object.__setattr__(self, "output", output.resolve())
        try:
            video_limit_bytes(self.video_limit_mib)
        except LumaBoxError:
            raise WebError("INVALID_CONFIG") from None

    @property
    def output_dir(self) -> Path:
        return self.output


class InstanceLock:
    """OS-backed lock: an exit releases it even after an unclean shutdown."""
    def __init__(self, directory: Path):
        self.directory, self.file = directory, None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "instance.lock"
        if path.exists() and is_link(path):
            raise WebError("INVALID_CONFIG")
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise WebError("ALREADY_RUNNING") from None
        return self

    def __exit__(self, *_):
        if self.file is not None:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
