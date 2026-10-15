from __future__ import annotations

import os
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import LumaBoxError
from ..urls import parse_input
from ..limits import DEFAULT_VIDEO_LIMIT_MIB, video_limit_bytes
from ..metadata import normalize_title
from ..progress import sanitize_snapshot
from .config import WebConfig, WebError, is_link

ID = re.compile(r"[0-9a-f]{32}")


class JobStore:
    def __init__(self, config: WebConfig):
        self.config = config
        self.lock = threading.RLock()
        self.downloads = {}
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        if is_link(config.output_dir):
            raise WebError("INVALID_CONFIG")
        self.database = config.data_dir / "jobs.sqlite3"
        if self.database.exists() and is_link(self.database):
            raise WebError("INVALID_CONFIG")
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, url TEXT NOT NULL, work_id TEXT, kind TEXT,
                  status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  error TEXT, expected_count INTEGER DEFAULT 0, actual_count INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS files (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                  name TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                  mime TEXT NOT NULL, width INTEGER, height INTEGER, duration REAL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, definition in (("video_limit_mib", "INTEGER NOT NULL DEFAULT 100"),
                                     ("error_detail", "TEXT"), ("retry_of", "TEXT"),
                                     ("title", "TEXT"), ("progress", "TEXT NOT NULL DEFAULT '{}'"),
                                     ("started_at", "REAL"), ("hidden_at", "REAL")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            previous = db.execute("SELECT value FROM meta WHERE key='output_root'").fetchone()
            self.previous_output = Path(previous[0]) if previous else config.data_dir / "outputs"

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.database, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def create(self, text: str, video_limit_mib: int = DEFAULT_VIDEO_LIMIT_MIB, *, retry_of: str | None = None) -> dict:
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise WebError("INVALID_INPUT")
        try:
            video_limit_bytes(video_limit_mib)
            link = parse_input(text)
        except LumaBoxError:
            raise WebError("INVALID_INPUT") from None
        parts = urlsplit(link.url)
        url = (f"https://www.douyin.com/{'video' if link.kind == 'video' else 'note'}/{link.work_id}"
               if link.work_id else f"https://v.douyin.com{parts.path}")
        job_id, now = uuid.uuid4().hex, time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0] >= 10:
                raise WebError("QUEUE_FULL", 409)
            if retry_of and db.execute("SELECT 1 FROM jobs WHERE retry_of=? AND status IN ('pending','running')", (retry_of,)).fetchone():
                raise WebError("ALREADY_QUEUED", 409)
            db.execute("INSERT INTO jobs (id,url,work_id,kind,status,created_at,updated_at,video_limit_mib,retry_of) VALUES (?,?,?,?,?,?,?,?,?)",
                       (job_id, url, link.work_id, link.kind, "pending", now, now, video_limit_mib, retry_of))
        return self.get(job_id)

    def retry(self, job_id: str, video_limit_mib: int = DEFAULT_VIDEO_LIMIT_MIB) -> dict:
        self.get(job_id)  # Validate the ID before querying the saved link.
        with self.lock:
            with self.connection() as db:
                row = db.execute("SELECT url,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row["status"] not in ("failed", "expired"):
                raise WebError("NOT_RETRYABLE", 409)
            return self.create(row["url"], video_limit_mib, retry_of=job_id)

    def claim(self) -> dict | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE status='pending' AND hidden_at IS NULL ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            now = time.time()
            db.execute("UPDATE jobs SET status='running',updated_at=?,started_at=? WHERE id=?", (now, now, row["id"]))
            return {**dict(row), "status": "running", "started_at": now}

    def get(self, job_id: str) -> dict:
        if not ID.fullmatch(job_id):
            raise WebError("NOT_FOUND", 404)
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND hidden_at IS NULL", (job_id,)).fetchone()
            if row is None:
                raise WebError("NOT_FOUND", 404)
            result = {key: row[key] for key in ("id", "work_id", "kind", "status", "created_at", "updated_at",
                                               "error", "expected_count", "actual_count")}
            result.update(source_url=row["url"], video_limit_mib=row["video_limit_mib"],
                          error_detail=json.loads(row["error_detail"] or "{}"), output_directory=None, files=[],
                          title=normalize_title(row["title"]), progress=json.loads(row["progress"] or "{}"))
            if row["started_at"] is not None:
                end = time.time() if row["status"] == "running" else row["updated_at"]
                result["progress"]["elapsed_seconds"] = max(0, int(end - row["started_at"]))
            if row["status"] == "success":
                result["files"] = [self.public_file(f) for f in db.execute("SELECT * FROM files WHERE job_id=? ORDER BY rowid", (job_id,))]
                first = db.execute("SELECT relative_path FROM files WHERE job_id=? ORDER BY rowid LIMIT 1", (job_id,)).fetchone()
                if first:
                    try:
                        result["output_directory"] = str(self.safe_path(str(Path(first[0]).parent)))
                    except WebError:
                        pass
            return result

    def list(self) -> list[dict]:
        with self.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE hidden_at IS NULL ORDER BY created_at DESC,rowid DESC LIMIT 30")]
        return [self.get(job_id) for job_id in ids]

    def search(self, q="", status="all", page=1, page_size=20) -> dict:
        if (not isinstance(q, str) or len(q) > 200 or
                status not in ("all", "pending", "running", "success", "failed", "expired") or
                type(page) is not int or not 1 <= page <= 1_000_000 or
                type(page_size) is not int or not 1 <= page_size <= 100):
            raise WebError("INVALID_REQUEST")
        clauses, parameters = ["hidden_at IS NULL"], []
        if status != "all":
            clauses.append("status=?")
            parameters.append(status)
        if q.strip():
            pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(COALESCE(title,'') LIKE ? ESCAPE '\\' OR work_id LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
            parameters.extend([pattern] * 3)
        where = " AND ".join(clauses)
        with self.lock:
            with self.connection() as db:
                total = db.execute(f"SELECT count(*) FROM jobs WHERE {where}", parameters).fetchone()[0]
                ids = [r[0] for r in db.execute(f"SELECT id FROM jobs WHERE {where} ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                       [*parameters, page_size, (page - 1) * page_size])]
            jobs = [self.get(job_id) for job_id in ids]
        return {"jobs": jobs, "total": total, "page": page, "page_size": page_size}

    def remove(self, job_id: str) -> int:
        with self.lock:
            job = self.get(job_id)
            if job["status"] in ("pending", "running"):
                raise WebError("JOB_ACTIVE", 409)
            with self.connection() as db:
                return db.execute("UPDATE jobs SET hidden_at=? WHERE id=? AND hidden_at IS NULL", (time.time(), job_id)).rowcount

    def clear_failed(self) -> int:
        with self.connection() as db:
            return db.execute("UPDATE jobs SET hidden_at=? WHERE status='failed' AND hidden_at IS NULL", (time.time(),)).rowcount

    def update_progress(self, job_id: str, snapshot: dict) -> bool:
        if not isinstance(job_id, str) or not ID.fullmatch(job_id) or not isinstance(snapshot, dict):
            return False
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND status='running' AND hidden_at IS NULL", (job_id,)).fetchone()
            if row is None:
                return False
            previous = json.loads(row["progress"] or "{}")
            progress = sanitize_snapshot({**previous, **snapshot})
            if progress is None:
                return False
            work_id, kind = progress["work_id"], progress["kind"]
            if (work_id is not None and kind is None) or (kind is not None and work_id is None):
                return False
            if row["work_id"] and work_id and (row["work_id"] != work_id or row["kind"] != kind):
                return False
            if (progress["bytes_received"] < previous.get("bytes_received", 0) or
                    progress["completed_count"] < previous.get("completed_count", 0) or
                    (previous.get("expected_count", 0) and progress["expected_count"] != previous["expected_count"]) or
                    (previous.get("stage") == "publication" and progress["stage"] != "publication")):
                return False
            title = normalize_title(progress["title"]) if work_id is not None else None
            db.execute("UPDATE jobs SET progress=?,work_id=COALESCE(work_id,?),kind=COALESCE(kind,?),title=COALESCE(?,title),updated_at=? WHERE id=?",
                       (json.dumps(progress, ensure_ascii=False), work_id, kind, title, time.time(), job_id))
        return True

    @staticmethod
    def public_file(file: dict) -> dict:
        return {key: file[key] for key in ("id", "name", "bytes", "mime", "width", "height", "duration")}

    def finish(self, job_id: str, result: dict, files: list[dict]):
        with self.connection() as db:
            row = db.execute("SELECT status,work_id,kind FROM jobs WHERE id=? AND hidden_at IS NULL", (job_id,)).fetchone()
            if (row is None or row["status"] != "running" or
                    (row["work_id"] and (row["work_id"] != result["work_id"] or row["kind"] != result["kind"]))):
                raise WebError("VERIFY_FAILED")
            for file in files:
                db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    uuid.uuid4().hex, job_id, file["relative_path"], file["name"], file["bytes"], file["sha256"],
                    file["mime"], file.get("width"), file.get("height"), file.get("duration")))
            db.execute("UPDATE jobs SET status='success',work_id=?,kind=?,title=COALESCE(?,title),expected_count=?,actual_count=?,updated_at=? WHERE id=?",
                       (result["work_id"], result["kind"], normalize_title(result.get("title")), result["expected_count"], result["actual_count"], time.time(), job_id))

    def fail(self, job_id: str, error: str, result: dict | None = None):
        result = result if isinstance(result, dict) else {}
        diagnostics = result.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        detail = {key: diagnostics[key] for key in ("limit_bytes", "actual_bytes", "at_least_bytes")
                  if type(diagnostics.get(key)) is int and 0 <= diagnostics[key] < 2**63}
        with self.connection() as db:
            row = db.execute("SELECT work_id,kind,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in ("pending", "running"):
                return
            work_id, kind = result.get("work_id"), result.get("kind")
            if (isinstance(work_id, str) and re.fullmatch(r"\d{15,20}", work_id) and kind in ("video", "gallery") and
                    row and (row["work_id"] is None or (row["work_id"] == work_id and row["kind"] == kind))):
                db.execute("UPDATE jobs SET work_id=?,kind=?,title=COALESCE(?,title) WHERE id=?", (work_id, kind, normalize_title(result.get("title")), job_id))
            db.execute("UPDATE jobs SET status='failed',error=?,error_detail=?,updated_at=? WHERE id=? AND status IN ('pending','running')",
                       (error, json.dumps(detail), time.time(), job_id))

    def recover(self):
        with self.connection() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE status='running'")]
        for job_id in ids:
            self.fail(job_id, "INTERRUPTED")
            self.try_cleanup(job_id)

    def file(self, file_id: str) -> dict:
        if not ID.fullmatch(file_id):
            raise WebError("NOT_FOUND", 404)
        with self.connection() as db:
            row = db.execute("SELECT files.* FROM files JOIN jobs ON jobs.id=files.job_id WHERE files.id=? AND jobs.status='success' AND jobs.hidden_at IS NULL", (file_id,)).fetchone()
            if row is None:
                raise WebError("NOT_FOUND", 404)
            return dict(row)

    def safe_path(self, relative: str, *, root: Path | None = None) -> Path:
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in ("..", ".") for p in parts):
            raise WebError("NOT_FOUND", 404)
        root = self.config.output_dir if root is None else root
        current = root
        try:
            for part in ("", *parts):
                current = current / part
                if is_link(current):
                    raise WebError("NOT_FOUND", 404)
            if not current.resolve().is_relative_to(root):
                raise WebError("NOT_FOUND", 404)
        except OSError:
            raise WebError("NOT_FOUND", 404) from None
        return current

    def folder(self, job_id: str) -> Path:
        job = self.get(job_id)
        if job["status"] != "success" or not job["output_directory"]:
            raise WebError("NOT_FOUND", 404)
        directory = Path(job["output_directory"])
        # Revalidate each file before asking the OS to open this job's directory.
        for public in job["files"]:
            self.file_path(self.file(public["id"]))
        return directory

    def migrate_outputs(self):
        old, new = self.previous_output, self.config.output_dir
        if old != new:
            with self.connection() as db:
                rows = [dict(row) for row in db.execute("SELECT files.* FROM files JOIN jobs ON files.job_id=jobs.id WHERE jobs.status='success'")]
            grouped = {}
            for row in rows:
                grouped.setdefault(row["job_id"], []).append(row)
            for job_id, files in grouped.items():
                if not ID.fullmatch(job_id) or new.is_relative_to(old / job_id):
                    raise WebError("MIGRATION_FAILED")
                for file in files:
                    parts = Path(file["relative_path"]).parts
                    if len(parts) < 2 or parts[0] != job_id:
                        raise WebError("MIGRATION_FAILED")

            def verify(files, root):
                try:
                    for file in files:
                        source = self.safe_path(file["relative_path"], root=root)
                        with source.open("rb") as stream:
                            if source.stat().st_size != file["bytes"] or hashlib.file_digest(stream, "sha256").hexdigest() != file["sha256"]:
                                raise WebError("MIGRATION_FAILED")
                except (OSError, WebError):
                    raise WebError("MIGRATION_FAILED") from None

            for job_id, files in grouped.items():
                destination = new / job_id
                root = new if destination.exists() else old
                verify(files, root)
                if destination.exists():
                    continue
                source = self.safe_path(job_id, root=old)
                for directory, dirs, names in os.walk(source, followlinks=False):
                    if any(is_link(Path(directory) / name) for name in dirs + names):
                        raise WebError("MIGRATION_FAILED")
                temporary = new / f".migration-{uuid.uuid4().hex}"
                try:
                    temporary.mkdir()
                    staged = temporary / job_id
                    shutil.copytree(source, staged)
                    if temporary.resolve().parent != new or destination.parent.resolve() != new:
                        raise WebError("MIGRATION_FAILED")
                    verify(files, temporary)
                    staged.rename(destination)
                finally:
                    if temporary.exists() and temporary.resolve().parent == new and not is_link(temporary):
                        shutil.rmtree(temporary)
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('output_root',?)", (str(new),))
        self.previous_output = new

    def file_path(self, file: dict) -> Path:
        path = self.safe_path(file["relative_path"])
        if not path.is_file() or path.stat().st_size != file["bytes"]:
            raise WebError("NOT_FOUND", 404)
        return path

    @contextmanager
    def open_file(self, file_id: str):
        # Acquire the lease before expiry can remove the directory or file record.
        with self.lock:
            file = self.file(file_id)
            try:
                stream = self.file_path(file).open("rb")
            except OSError:
                raise WebError("NOT_FOUND", 404) from None
            job_id = file["job_id"]
            self.downloads[job_id] = self.downloads.get(job_id, 0) + 1
        try:
            yield file, stream
        finally:
            stream.close()
            with self.lock:
                self.downloads[job_id] -= 1
                if not self.downloads[job_id]:
                    del self.downloads[job_id]

    def try_cleanup(self, job_id: str) -> bool:
        try:
            self.cleanup(job_id)
            return True
        except (OSError, WebError):
            logging.getLogger(__name__).warning("Private output cleanup deferred; will retry at expiry.")
            return False

    def cleanup(self, job_id: str):
        if not ID.fullmatch(job_id):
            raise WebError("INVALID_CONFIG")
        path = self.config.output_dir / job_id
        if not path.exists():
            return
        if is_link(self.config.output_dir) or is_link(path) or path.resolve().parent != self.config.output_dir:
            raise WebError("INVALID_CONFIG")
        # Never traverse junctions or symlinks. Unlinking the link leaves its target intact.
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in list(dirs) + files:
                child = Path(directory) / name
                if is_link(child):
                    if child.is_dir() and os.name == "nt":
                        child.rmdir()
                    else:
                        child.unlink()
                    if name in dirs:
                        dirs.remove(name)
        shutil.rmtree(path)

    def usage(self) -> int:
        if is_link(self.config.output_dir):
            raise WebError("INVALID_CONFIG")
        total = 0
        for directory, dirs, files in os.walk(self.config.output_dir, followlinks=False):
            dirs[:] = [name for name in dirs if not is_link(Path(directory) / name)]
            for name in files:
                path = Path(directory) / name
                if not is_link(path):
                    total += path.stat().st_size
        return total

    def expire(self, now: float | None = None):
        if self.config.retention_seconds is None:
            with self.connection() as db:
                failed = [row[0] for row in db.execute("SELECT id FROM jobs WHERE status='failed'")]
            for job_id in failed:
                self.try_cleanup(job_id)
            return  # A local collection is permanent; only incomplete outputs are cleaned.
        now = time.time() if now is None else now
        with self.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('success','failed') AND updated_at<?",
                                            (now - self.config.retention_seconds,))]
        for job_id in ids:
            with self.lock:
                if self.downloads.get(job_id) or not self.try_cleanup(job_id):
                    continue
                with self.connection() as db:
                    db.execute("UPDATE jobs SET status='expired',updated_at=? WHERE id=?", (now, job_id))
                    db.execute("DELETE FROM files WHERE job_id=?", (job_id,))
