"""Private atomic progress snapshots; no media addresses or browser state."""
import json
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path

from .metadata import normalize_title

STAGES = frozenset({"browser_start", "browser_load", "media_transfer", "media_verification", "publication"})
MAX_SNAPSHOT_BYTES = 16 * 1024
MAX_BYTES = 2**53 - 1


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def validate_progress_path(output: Path, path: Path) -> Path:
    target = Path(path).absolute()
    if target.name != ".progress.json" or target.parent.resolve() != Path(output).resolve():
        raise ValueError("Progress must use the output directory's .progress.json")
    if target.exists() and (_is_link(target) or not target.is_file()):
        raise ValueError("Progress destination must be a regular file")
    # Use the same resolved root as the download, never the user-supplied alias.
    return Path(output).resolve() / ".progress.json"


def sanitize_snapshot(value: object) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("stage"), str) or value["stage"] not in STAGES:
        return None
    work_id, kind = value.get("work_id"), value.get("kind")
    if work_id is not None and (not isinstance(work_id, str) or not re.fullmatch(r"[0-9]{15,20}", work_id)):
        return None
    if kind is not None and kind not in ("video", "gallery"):
        return None
    result = {"stage": value["stage"], "work_id": work_id, "kind": kind,
        "title": normalize_title(value.get("title"))}
    for key, limit, default in (("bytes_received", MAX_BYTES, 0), ("bytes_total", MAX_BYTES, None),
            ("completed_count", 50, 0), ("expected_count", 50, 0), ("resource_index", 50, None)):
        number = value.get(key, default)
        if number is None and key in ("bytes_total", "resource_index"):
            result[key] = None
            continue
        if type(number) is not int or not 0 <= number <= limit or (key == "resource_index" and number == 0):
            return None
        result[key] = number
    if (result["completed_count"] > result["expected_count"] or
            (result["resource_index"] is not None and result["resource_index"] > result["expected_count"]) or
            (result["bytes_total"] is not None and result["bytes_received"] > result["bytes_total"])):
        return None
    if kind == "gallery":
        result["bytes_total"] = None
    return result


def read_progress(path: Path) -> dict | None:
    try:
        if _is_link(path) or not path.is_file():
            return None
        with path.open("rb") as source:
            data = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            return None
        return sanitize_snapshot(json.loads(data))
    except (OSError, ValueError, RecursionError, TypeError):
        return None


class ProgressFile:
    def __init__(self, path: Path, *, clock=time.monotonic):
        self.path, self.clock = Path(path), clock
        self.lock = threading.Lock()
        self.snapshot = {}
        self.last_write = float("-inf")

    def emit(self, **updates) -> bool:
        with self.lock:
            snapshot = sanitize_snapshot({**self.snapshot, **updates})
            if snapshot is None:
                return False
            phase_changed = snapshot["stage"] != self.snapshot.get("stage")
            self.snapshot = snapshot
            now = self.clock()
            if not phase_changed and now - self.last_write < .5:
                return False
            self.last_write = now
            temporary = None
            try:
                if self.path.exists() and (_is_link(self.path) or not self.path.is_file()):
                    return False
                data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(data) > MAX_SNAPSHOT_BYTES:
                    return False
                with tempfile.NamedTemporaryFile("wb", dir=self.path.parent, prefix=".progress-", suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(data)
                    output.flush()
                os.replace(temporary, self.path)
                return True
            except OSError:
                return False  # Display metadata cannot turn a verified download into failure.
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
