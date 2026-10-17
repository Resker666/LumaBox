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
from ..metadata import normalize_title
from ..progress import read_progress
from ..diagnostics import public_diagnostics

from .config import InstanceLock, WebError
from .process import ProcessTree
from .store import ID
from .retry_policy import should_retry

ERRORS = frozenset({"INVALID_INPUT", "UNSAFE_TARGET", "AUTH_REQUIRED", "BLOCKED", "NOT_FOUND",
                    "RATE_LIMITED", "UNSUPPORTED_MEDIA", "PARSE_CHANGED", "VERIFY_FAILED", "SIZE_LIMIT",
                    "DEPENDENCY_MISSING", "INTERRUPTED", "DOWNLOAD_FAILED", "TIMEOUT", "STORAGE_FULL",
                    "CHALLENGE_REQUIRED", "RESOURCE_TIMEOUT", "CLEANUP_FAILED"})


class AttemptFailure(WebError):
    def __init__(self, code, result):
        super().__init__(code)
        self.result = result


class DuplicateResult(Exception):
    def __init__(self, target_id):
        self.target_id = target_id


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
        command.extend(["--progress-file", str(directory / ".progress.json")])
        if self.config.headless:
            command.append("--headless")
        return command

    def _read_progress(self, path):
        try:
            safe = self.store.safe_path(str(path.relative_to(self.config.output_dir)))
        except (WebError, ValueError):
            return None
        return read_progress(safe)

    def run_once(self) -> bool:
        if self.stopping.is_set():
            return False
        job = self.store.claim()
        if job is None:
            return False
        while job is not None:
            if job["status"] == "retry_wait":
                job = self._wait_retry(job)
                if job is None:
                    break
            try:
                self._run_attempt(job)
                break
            except DuplicateResult as duplicate:
                cleaned = self.store.try_cleanup(job["id"])
                if not cleaned or not self.store.mark_duplicate(job["id"], duplicate.target_id):
                    self.store.fail(job["id"], "INTERRUPTED" if self.store.is_cancelling(job["id"]) else "VERIFY_FAILED")
                break
            except AttemptFailure as exc:
                cleaned = self.store.try_cleanup(job["id"])
                if self.stopping.is_set() or self.store.is_cancelling(job["id"]):
                    self.store.fail(job["id"], "INTERRUPTED", exc.result)
                elif not cleaned and should_retry(exc.code, public_diagnostics(exc.result.get("diagnostics"))):
                    self.store.fail(job["id"], "CLEANUP_FAILED", exc.result)
                elif cleaned and self.store.schedule_retry(job["id"], exc.code, exc.result):
                    job = self.store.claim()
                    continue
                else:
                    self.store.fail(job["id"], exc.code, exc.result)
                break
        return True

    def _wait_retry(self, job):
        while not self.stopping.is_set():
            current = self.store.get(job["id"])
            if current["status"] != "retry_wait":
                return None
            now = time.time()
            if now >= job["deadline_at"]:
                self.store.fail(job["id"], "TIMEOUT")
                return None
            if now >= current["retry_at"]:
                return self.store.begin_retry(job["id"])
            self.stopping.wait(min(.1, current["retry_at"] - now))
        return None

    def _run_attempt(self, job):
        directory = self.config.output_dir / job["id"]
        progress_path = directory / ".progress.json"
        last_progress = None
        result = {}
        try:
            if self.stopping.is_set():
                raise WebError("INTERRUPTED")
            self._check_cancelled(job["id"])
            reserve = max(self.config.reserve_bytes, video_limit_bytes(job["video_limit_mib"]) + 64 * MIB, 2 * GALLERY_LIMIT + 64 * MIB)
            if ((self.config.capacity_bytes is not None and self.store.usage() + reserve > self.config.capacity_bytes) or
                    shutil.disk_usage(self.config.output_dir).free < reserve):
                raise WebError("STORAGE_FULL")
            directory.mkdir()
            output = directory / "cli-result.jsonl"
            with output.open("xb") as stdout:
                with ProcessTree(self.command(job, directory), stdout) as tree:
                    process = tree.process
                    while process.poll() is None:
                        if self.stopping.wait(.05):
                            raise WebError("INTERRUPTED")
                        self._check_cancelled(job["id"])
                        if output.stat().st_size > 256 * 1024:
                            raise WebError("VERIFY_FAILED")
                        if self.config.capacity_bytes is not None and self.store.usage() > self.config.capacity_bytes:
                            raise WebError("STORAGE_FULL")
                        snapshot = self._read_progress(progress_path)
                        if snapshot is not None and snapshot != last_progress:
                            if self.store.update_progress(job["id"], snapshot):
                                self._check_duplicate(job, snapshot)
                            last_progress = snapshot
            if self.stopping.is_set():
                raise WebError("INTERRUPTED")
            self._check_cancelled(job["id"])
            if output.stat().st_size > 256 * 1024:
                raise WebError("VERIFY_FAILED")
            snapshot = self._read_progress(progress_path)
            if snapshot is not None and snapshot != last_progress:
                self.store.update_progress(job["id"], snapshot)
                last_progress = snapshot
            decoded = json.loads(output.read_bytes().splitlines()[-1])
            if not isinstance(decoded, dict):
                raise WebError("VERIFY_FAILED")
            result = decoded
            if process.returncode != 0 or result.get("status") != "success":
                code = result.get("error")
                raise WebError(code if code in ERRORS else "DOWNLOAD_FAILED")
            self.store.update_progress(job["id"], {**(last_progress or {}), "stage": "publication", "resource_index": None})
            files = self._verify(job, directory, result)
            self._check_duplicate(job, result)
            if self.config.capacity_bytes is not None and self.store.usage() > self.config.capacity_bytes:
                raise WebError("STORAGE_FULL")
            self.store.update_progress(job["id"], {"stage": "publication", "work_id": result["work_id"],
                "kind": result["kind"], "title": normalize_title(result.get("title")),
                "bytes_received": result["bytes"], "bytes_total": result["bytes"] if result["kind"] == "video" else None,
                "completed_count": result["actual_count"], "expected_count": result["expected_count"], "resource_index": None})
            for auxiliary in (output, progress_path):
                try:
                    auxiliary.unlink(missing_ok=True)
                except OSError:
                    pass  # Private auxiliary files are not part of publication.
            self.store.finish(job["id"], result, files)
        except DuplicateResult:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, WebError) and exc.code in ERRORS else "VERIFY_FAILED"
            raise AttemptFailure(code, result) from None

    def _check_duplicate(self, job, identity):
        if identity.get("work_id") and identity.get("kind"):
            match = self.store.check_duplicate(job["id"], identity["work_id"], identity["kind"])
            if match:
                raise DuplicateResult(match["id"])

    def _check_cancelled(self, job_id):
        if self.store.is_cancelling(job_id):
            raise WebError("INTERRUPTED")
        with self.store.connection() as db:
            row = db.execute("SELECT deadline_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is not None and row["deadline_at"] is not None and time.time() >= row["deadline_at"]:
            raise WebError("TIMEOUT")

    def _digest(self, stream, job_id):
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            self._check_cancelled(job_id)
            digest.update(block)
        return digest.hexdigest()

    def _verify(self, job, directory, result):
        work_id, run_id, kind = result["work_id"], result["run_id"], result["kind"]
        if (not isinstance(work_id, str) or not re.fullmatch(r"\d{15,20}", work_id) or
                not isinstance(run_id, str) or not ID.fullmatch(run_id) or kind not in ("video", "gallery") or
                (job["work_id"] and (work_id != job["work_id"] or kind != job["kind"])) or
                result.get("metadata_parsed") is not True or result.get("checks", {}).get("full_decode") is not True):
            raise WebError("VERIFY_FAILED")
        entries = result["files"]
        expected = result["expected_count"]
        if (not isinstance(entries, list) or not 1 <= len(entries) <= 100 or
                type(expected) is not int or not 1 <= expected <= 50 or result["actual_count"] != expected or
                not expected <= len(entries) <= 2 * expected or
                (kind == "video" and (len(entries) != 1 or expected != 1))):
            raise WebError("VERIFY_FAILED")
        dest = directory / f"{work_id}-{run_id}"
        if Path(result["output_directory"]) != dest or Path(result["report_path"]) != dest / "report.json":
            raise WebError("VERIFY_FAILED")
        relative_dir = dest.relative_to(self.config.output_dir)
        report = self.store.safe_path(str(relative_dir / "report.json"))
        if report.stat().st_size > 256 * 1024 or json.loads(report.read_bytes()) != result:
            raise WebError("VERIFY_FAILED")
        files = []
        image_count, motion_orders = 0, set()
        for entry in entries:
            self._check_cancelled(job["id"])
            name = entry["name"]
            is_video = kind == "video"
            if kind == "video":
                valid_name = name == f"{work_id}.mp4"
            elif re.fullmatch(fr"{image_count + 1:02d}\.(jpg|webp)", name):
                image_count += 1
                valid_name = True
            else:
                valid_name = image_count > 0 and name == f"{image_count:02d}-motion.mp4" and image_count not in motion_orders
                if valid_name:
                    motion_orders.add(image_count)
                    is_video = True
            if not valid_name or type(entry["bytes"]) is not int or entry["bytes"] <= 0:
                raise WebError("VERIFY_FAILED")
            path = self.store.safe_path(str(relative_dir / name))
            limit = video_limit_bytes(job["video_limit_mib"]) if is_video else IMAGE_LIMIT
            if (path.stat().st_size != entry["bytes"] or entry["bytes"] > limit or
                    not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise WebError("VERIFY_FAILED")
            with path.open("rb") as stream:
                if self._digest(stream, job["id"]) != entry["sha256"]:
                    raise WebError("VERIFY_FAILED")
            if any(type(entry[k]) is not int or entry[k] <= 0 for k in ("width", "height")):
                raise WebError("VERIFY_FAILED")
            duration = entry["duration"]
            if is_video and (type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0):
                raise WebError("VERIFY_FAILED")
            if not is_video and duration is not None:
                raise WebError("VERIFY_FAILED")
            files.append({**entry, "relative_path": str(relative_dir / name),
                          "mime": "video/mp4" if is_video else "image/webp" if name.endswith(".webp") else "image/jpeg"})
        if kind == "gallery" and image_count != expected:
            raise WebError("VERIFY_FAILED")
        if sum(f["bytes"] for f in files) != result["bytes"] or (kind == "gallery" and result["bytes"] > GALLERY_LIMIT):
            raise WebError("VERIFY_FAILED")
        if kind == "gallery":
            archive = self.store.safe_path(str(relative_dir / f"{work_id}-images.zip"))
            with zipfile.ZipFile(archive) as original:
                if original.namelist() != [f["name"] for f in files] + ["report.json"]:
                    raise WebError("VERIFY_FAILED")
                for file in files:
                    self._check_cancelled(job["id"])
                    info = original.getinfo(file["name"])
                    if info.file_size != file["bytes"] or hashlib.sha256(original.read(info)).hexdigest() != file["sha256"]:
                        raise WebError("VERIFY_FAILED")
            # The CLI's private report contains local paths. Publish a minimal portable report instead.
            public_report = {key: result[key] for key in ("work_id", "kind", "status", "expected_count", "actual_count", "bytes", "checks")}
            public_report["title"] = normalize_title(result.get("title"))
            public_report["files"] = [{key: file[key] for key in ("name", "bytes", "sha256", "width", "height", "duration")} for file in files]
            archive.unlink()  # Verified, rebuildable ZIP; avoid a third full media copy.
            replacement = dest / "images.tmp"
            with zipfile.ZipFile(replacement, "x", compression=zipfile.ZIP_STORED) as output:
                for file in files:
                    self._check_cancelled(job["id"])
                    output.write(dest / file["name"], file["name"])
                    if self.config.capacity_bytes is not None and self.store.usage() > self.config.capacity_bytes:
                        raise WebError("STORAGE_FULL")
                output.writestr("report.json", json.dumps(public_report, ensure_ascii=False, indent=2))
            replacement.replace(archive)
            with archive.open("rb") as stream:
                digest = self._digest(stream, job["id"])
            files.append({"name": archive.name, "bytes": archive.stat().st_size, "sha256": digest,
                          "relative_path": str(archive.relative_to(self.config.output_dir)), "mime": "application/zip"})
        return files
