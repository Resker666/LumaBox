import hashlib
import json
import math
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from ..limits import GALLERY_LIMIT, IMAGE_LIMIT, MIB, video_limit_bytes

from .config import InstanceLock, WebError
from .process import ProcessTree
from .store import ID

ERRORS = frozenset({"INVALID_INPUT", "UNSAFE_TARGET", "AUTH_REQUIRED", "BLOCKED", "NOT_FOUND",
                    "RATE_LIMITED", "UNSUPPORTED_MEDIA", "PARSE_CHANGED", "VERIFY_FAILED", "SIZE_LIMIT",
                    "DEPENDENCY_MISSING", "INTERRUPTED", "DOWNLOAD_FAILED", "TIMEOUT", "STORAGE_FULL",
                    "CHALLENGE_REQUIRED", "RESOURCE_TIMEOUT"})


class JobWorker:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.stopping = threading.Event()
        self.thread = None
        self.instance = None

    def start(self):
        self.instance = InstanceLock(self.config.data_dir)
        self.instance.__enter__()
        try:
            self.store.migrate_outputs()
            self.store.recover()
            self.thread = threading.Thread(target=self._loop, name="lumabox-download", daemon=False)
            self.thread.start()
        except BaseException:
            self.instance.__exit__()
            raise

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise WebError("INTERRUPTED")
        if self.instance is not None:
            self.instance.__exit__()
            self.instance = None

    def _loop(self):
        next_cleanup = 0
        while not self.stopping.is_set():
            if time.monotonic() >= next_cleanup:
                self.store.expire()
                next_cleanup = time.monotonic() + 60
            if not self.run_once():
                self.stopping.wait(.5)

    def command(self, job, directory):
        command = [sys.executable, "-m", "lumabox", "--authorized", job["url"], "--output", str(directory)]
        command.extend(["--video-limit-mib", str(job["video_limit_mib"])])
        if self.config.headless:
            command.append("--headless")
        return command

    def run_once(self) -> bool:
        if self.stopping.is_set():
            return False
        job = self.store.claim()
        if job is None:
            return False
        directory = self.config.output_dir / job["id"]
        result = {}
        try:
            reserve = max(self.config.reserve_bytes, video_limit_bytes(job["video_limit_mib"]) + 64 * MIB, 2 * GALLERY_LIMIT + 64 * MIB)
            if ((self.config.capacity_bytes is not None and self.store.usage() + reserve > self.config.capacity_bytes) or
                    shutil.disk_usage(self.config.output_dir).free < reserve):
                raise WebError("STORAGE_FULL")
            directory.mkdir()
            output = directory / "cli-result.jsonl"
            with output.open("xb") as stdout:
                with ProcessTree(self.command(job, directory), stdout) as tree:
                    process = tree.process
                    start = time.monotonic()
                    while process.poll() is None:
                        if self.stopping.wait(.05):
                            raise WebError("INTERRUPTED")
                        if time.monotonic() - start > self.config.task_timeout:
                            raise WebError("TIMEOUT")
                        if output.stat().st_size > 256 * 1024:
                            raise WebError("VERIFY_FAILED")
                        if self.config.capacity_bytes is not None and self.store.usage() > self.config.capacity_bytes:
                            raise WebError("STORAGE_FULL")
            if self.stopping.is_set():
                raise WebError("INTERRUPTED")
            if output.stat().st_size > 256 * 1024:
                raise WebError("VERIFY_FAILED")
            result = json.loads(output.read_bytes().splitlines()[-1])
            if process.returncode != 0 or result.get("status") != "success":
                code = result.get("error")
                raise WebError(code if code in ERRORS else "DOWNLOAD_FAILED")
            files = self._verify(job, directory, result)
            if self.config.capacity_bytes is not None and self.store.usage() > self.config.capacity_bytes:
                raise WebError("STORAGE_FULL")
            try:
                output.unlink()
            except OSError:
                pass  # Private auxiliary log is not part of publication.
            self.store.finish(job["id"], result, files)
        except Exception as exc:
            code = exc.code if isinstance(exc, WebError) and exc.code in ERRORS else "VERIFY_FAILED"
            self.store.fail(job["id"], code, result)
            self.store.try_cleanup(job["id"])
        return True

    def _verify(self, job, directory, result):
        work_id, run_id, kind = result["work_id"], result["run_id"], result["kind"]
        if (not isinstance(work_id, str) or not re.fullmatch(r"\d{15,20}", work_id) or
                not isinstance(run_id, str) or not ID.fullmatch(run_id) or kind not in ("video", "gallery") or
                (job["work_id"] and (work_id != job["work_id"] or kind != job["kind"])) or
                result.get("metadata_parsed") is not True or result.get("checks", {}).get("full_decode") is not True):
            raise WebError("VERIFY_FAILED")
        entries = result["files"]
        if (not isinstance(entries, list) or not 1 <= len(entries) <= 100 or
                result["expected_count"] != len(entries) or result["actual_count"] != len(entries) or
                (kind == "video" and len(entries) != 1)):
            raise WebError("VERIFY_FAILED")
        dest = directory / f"{work_id}-{run_id}"
        if Path(result["output_directory"]) != dest or Path(result["report_path"]) != dest / "report.json":
            raise WebError("VERIFY_FAILED")
        relative_dir = dest.relative_to(self.config.output_dir)
        report = self.store.safe_path(str(relative_dir / "report.json"))
        if report.stat().st_size > 256 * 1024 or json.loads(report.read_bytes()) != result:
            raise WebError("VERIFY_FAILED")
        files = []
        for index, entry in enumerate(entries, 1):
            name = entry["name"]
            valid_name = name == f"{work_id}.mp4" if kind == "video" else bool(re.fullmatch(fr"{index:02d}\.(jpg|webp)", name))
            if not valid_name or type(entry["bytes"]) is not int or entry["bytes"] <= 0:
                raise WebError("VERIFY_FAILED")
            path = self.store.safe_path(str(relative_dir / name))
            limit = video_limit_bytes(job["video_limit_mib"]) if kind == "video" else IMAGE_LIMIT
            if (path.stat().st_size != entry["bytes"] or entry["bytes"] > limit or
                    not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise WebError("VERIFY_FAILED")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise WebError("VERIFY_FAILED")
            if any(type(entry[k]) is not int or entry[k] <= 0 for k in ("width", "height")):
                raise WebError("VERIFY_FAILED")
            duration = entry["duration"]
            if kind == "video" and (not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0):
                raise WebError("VERIFY_FAILED")
            files.append({**entry, "relative_path": str(relative_dir / name),
                          "mime": "video/mp4" if kind == "video" else "image/webp" if name.endswith(".webp") else "image/jpeg"})
        if sum(f["bytes"] for f in files) != result["bytes"] or (kind == "gallery" and result["bytes"] > GALLERY_LIMIT):
            raise WebError("VERIFY_FAILED")
        if kind == "gallery":
            archive = self.store.safe_path(str(relative_dir / f"{work_id}-images.zip"))
            with zipfile.ZipFile(archive) as original:
                if original.namelist() != [f["name"] for f in files] + ["report.json"]:
                    raise WebError("VERIFY_FAILED")
                for file in files:
                    info = original.getinfo(file["name"])
                    if info.file_size != file["bytes"] or hashlib.sha256(original.read(info)).hexdigest() != file["sha256"]:
                        raise WebError("VERIFY_FAILED")
            # The CLI's private report contains local paths. Publish a minimal portable report instead.
            public_report = {key: result[key] for key in ("work_id", "kind", "status", "expected_count", "actual_count", "bytes", "checks")}
            public_report["files"] = [{key: file[key] for key in ("name", "bytes", "sha256", "width", "height", "duration")} for file in files]
            archive.unlink()  # Verified, rebuildable ZIP; avoid a third full media copy.
            replacement = dest / "images.tmp"
            with zipfile.ZipFile(replacement, "x", compression=zipfile.ZIP_STORED) as output:
                for file in files:
                    output.write(dest / file["name"], file["name"])
                    if self.config.capacity_bytes is not None and self.store.usage() > self.config.capacity_bytes:
                        raise WebError("STORAGE_FULL")
                output.writestr("report.json", json.dumps(public_report, ensure_ascii=False, indent=2))
            replacement.replace(archive)
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append({"name": archive.name, "bytes": archive.stat().st_size, "sha256": digest,
                          "relative_path": str(archive.relative_to(self.config.output_dir)), "mime": "application/zip"})
        return files
