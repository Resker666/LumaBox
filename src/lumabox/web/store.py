from __future__ import annotations

import os
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
from .config import WebConfig, WebError, is_link

ID = re.compile(r"[0-9a-f]{32}")


class JobStore:
    def __init__(self, config: WebConfig):
        self.config = config
        self.lock = threading.RLock()
        self.downloads = {}
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.output_dir.mkdir(exist_ok=True)
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
            """)

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

    def create(self, text: str) -> dict:
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise WebError("INVALID_INPUT")
        try:
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
            db.execute("INSERT INTO jobs (id,url,work_id,kind,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (job_id, url, link.work_id, link.kind, "pending", now, now))
        return self.get(job_id)

    def claim(self) -> dict | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def get(self, job_id: str) -> dict:
        if not ID.fullmatch(job_id):
            raise WebError("NOT_FOUND", 404)
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise WebError("NOT_FOUND", 404)
            result = {key: row[key] for key in ("id", "work_id", "kind", "status", "created_at", "updated_at",
                                               "error", "expected_count", "actual_count")}
            result["files"] = []
            if row["status"] == "success":
                result["files"] = [self.public_file(f) for f in db.execute("SELECT * FROM files WHERE job_id=? ORDER BY rowid", (job_id,))]
            return result

    def list(self) -> list[dict]:
        with self.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 30")]
        return [self.get(job_id) for job_id in ids]

    @staticmethod
    def public_file(file: dict) -> dict:
        return {key: file[key] for key in ("id", "name", "bytes", "mime", "width", "height", "duration")}

    def finish(self, job_id: str, result: dict, files: list[dict]):
        with self.connection() as db:
            if db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] != "running":
                raise WebError("VERIFY_FAILED")
            for file in files:
                db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    uuid.uuid4().hex, job_id, file["relative_path"], file["name"], file["bytes"], file["sha256"],
                    file["mime"], file.get("width"), file.get("height"), file.get("duration")))
            db.execute("UPDATE jobs SET status='success',work_id=?,kind=?,expected_count=?,actual_count=?,updated_at=? WHERE id=?",
                       (result["work_id"], result["kind"], result["expected_count"], result["actual_count"], time.time(), job_id))

    def fail(self, job_id: str, error: str):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status IN ('pending','running')",
                       (error, time.time(), job_id))

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
            row = db.execute("SELECT files.* FROM files JOIN jobs ON jobs.id=files.job_id WHERE files.id=? AND jobs.status='success'", (file_id,)).fetchone()
            if row is None:
                raise WebError("NOT_FOUND", 404)
            return dict(row)

    def safe_path(self, relative: str) -> Path:
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in ("..", ".") for p in parts):
            raise WebError("NOT_FOUND", 404)
        current = self.config.output_dir
        try:
            for part in ("", *parts):
                current = current / part
                if is_link(current):
                    raise WebError("NOT_FOUND", 404)
            if not current.resolve().is_relative_to(self.config.output_dir):
                raise WebError("NOT_FOUND", 404)
        except OSError:
            raise WebError("NOT_FOUND", 404) from None
        return current

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
