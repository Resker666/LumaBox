import os
import stat
from dataclasses import dataclass
from pathlib import Path


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
    task_timeout: float = 900
    capacity_bytes: int = 2 * 1024**3
    reserve_bytes: int = 512 * 1024**2
    retention_seconds: int = 86400

    def __post_init__(self):
        if not 1 <= self.port <= 65535 or self.task_timeout <= 0:
            raise WebError("INVALID_CONFIG")
        original = Path(self.data_dir).absolute()
        if original.exists() and is_link(original):
            raise WebError("INVALID_CONFIG")
        object.__setattr__(self, "data_dir", original.resolve())

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "outputs"


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
