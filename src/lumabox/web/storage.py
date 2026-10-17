"""Current-output inventory; disk free space is not a LumaBox quota."""
import os
import json
import re
from pathlib import Path
import shutil
import threading
import time

from ..metadata import normalize_title
from .config import WebError, is_link
from .duplicates import ACTIVE


class StorageManager:
    def __init__(self, store):
        self.store = store
        self._scan_lock = threading.Lock()
        self._cache = None
        self._generation = 0

    def invalidate(self):
        with self.store.lock:
            self._generation += 1
            self._cache = None

    def _snapshot(self, refresh=False):
        with self._scan_lock:
            now = time.monotonic()
            if not refresh and self._cache is not None and now - self._cache[0] < 30:
                return self._cache[1]
            with self.store.connection() as db:
                generation = self._generation
                rows = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC")]
                files = [dict(r) for r in db.execute("SELECT * FROM files")]
            root = self.store.config.output_dir
            complete, lengths, groups = True, {}, {}

            def failed(_):
                nonlocal complete
                complete = False

            try:
                if is_link(root):
                    raise OSError("Output is a link")
                for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
                    for name in list(dirs):
                        try:
                            if is_link(Path(directory) / name):
                                dirs.remove(name); complete = False
                        except OSError:
                            dirs.remove(name); complete = False
                    for name in names:
                        path = Path(directory) / name
                        try:
                            if is_link(path) or not path.is_file():
                                complete = False
                                continue
                            size = path.stat().st_size
                            relative = path.relative_to(root)
                            lengths[str(relative)] = size
                            group = groups.setdefault(relative.parts[0], {"bytes": 0, "file_count": 0})
                            group["bytes"] += size; group["file_count"] += 1
                        except OSError:
                            complete = False
                disk_free = shutil.disk_usage(root).free
            except OSError:
                complete, disk_free = False, None
            jobs = {r["id"]: r for r in rows}
            saved = hidden = 0
            saved_ids = set()
            for file in files:
                row = jobs.get(file["job_id"])
                relative = Path(file["relative_path"])
                if (row and row["status"] == "success" and row["storage_state"] == "present" and
                        not relative.is_absolute() and relative.parts and relative.parts[0] == row["id"] and
                        ".." not in relative.parts and str(relative) in lengths):
                    size = lengths[str(relative)]
                    saved += size; saved_ids.add(row["id"])
                    if row["hidden_at"] is not None:
                        hidden += size
            items = []
            for row in rows:
                group = groups.get(row["id"], {"bytes": 0, "file_count": 0})
                if not group["file_count"] and row["storage_state"] not in {"deleting", "delete_failed"}:
                    continue
                items.append({key: row[key] for key in ("id", "work_id", "kind", "status", "created_at", "storage_state")})
                items[-1].update(group, title=normalize_title(row["title"]), hidden=row["hidden_at"] is not None)
            total = sum(lengths.values())
            snapshot = {"summary": {"output_bytes": total, "saved_bytes": saved, "hidden_bytes": hidden,
                "other_bytes": max(0, total - saved), "saved_count": len(saved_ids), "disk_free_bytes": disk_free,
                "sampled_at": time.time(), "complete": complete}, "items": items}
            with self.store.lock:
                if generation == self._generation:
                    self._cache = (now, snapshot)
            return snapshot

    def summary(self, *, refresh=False):
        return dict(self._snapshot(refresh)["summary"])

    def items(self, *, scope="all", page=1, page_size=20):
        if (scope not in {"all", "hidden"} or type(page) is not int or not 1 <= page <= 1_000_000 or
                type(page_size) is not int or not 1 <= page_size <= 100):
            raise WebError("INVALID_REQUEST")
        items = self._snapshot()["items"]
        if scope == "hidden":
            items = [item for item in items if item["hidden"]]
        return {"items": [dict(i) for i in items[(page - 1) * page_size:page * page_size]],
                "total": len(items), "page": page, "page_size": page_size}

    def _checked(self, relative):
        root = self.store.config.output_dir
        path = Path(relative)
        if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
            raise WebError("UNSAFE_DELETE", 409)
        target = root / path
        try:
            if root.resolve() != root or is_link(root):
                raise WebError("UNSAFE_DELETE", 409)
            current = root
            for part in path.parts:
                current /= part
                if os.path.lexists(current) and is_link(current):
                    raise WebError("UNSAFE_DELETE", 409)
            if not target.resolve().is_relative_to(root):
                raise WebError("UNSAFE_DELETE", 409)
        except OSError:
            raise WebError("UNSAFE_DELETE", 409) from None
        return target

    def _inventory(self, row, manifest=None):
        job_id = row["id"]
        root = self._checked(job_id)
        if manifest is None:
            allowed = {str(Path(job_id) / name) for name in ("cli-result.jsonl", ".progress.json")}
            with self.store.connection() as db:
                files = db.execute("SELECT * FROM files WHERE job_id=?", (job_id,)).fetchall()
            for file in files:
                relative = Path(file["relative_path"])
                if (relative.is_absolute() or len(relative.parts) != 3 or relative.parts[0] != job_id or
                        not re.fullmatch(fr"{re.escape(row['work_id'] or '')}-[0-9a-f]{{32}}", relative.parts[1]) or
                        relative.name != file["name"] or not re.fullmatch(
                            r"(?:[0-9]{15,20}(?:-images)?|[0-9]{2}(?:-motion)?)\.(?:mp4|zip|jpg|webp)", relative.name)):
                    raise WebError("UNSAFE_DELETE", 409)
                self._checked(str(relative))
                allowed.update({str(relative), str(relative.parent / "report.json")})
            if root.exists():
                for child in root.iterdir():
                    if re.fullmatch(r"[0-9a-f]{32}-failed\.json", child.name):
                        allowed.add(str(child.relative_to(self.store.config.output_dir)))
        else:
            if not isinstance(manifest, list) or len(manifest) > 150 or not all(isinstance(p, str) for p in manifest):
                raise WebError("UNSAFE_DELETE", 409)
            allowed = set(manifest)
            for relative in allowed:
                if len(Path(relative).parts) < 2 or Path(relative).parts[0] != job_id:
                    raise WebError("UNSAFE_DELETE", 409)
                self._checked(relative)
        directories, actual = [], []
        parents = {str(p) for relative in allowed for p in Path(relative).parents if str(p) != "."}
        if not root.exists():
            return actual, directories
        def fail(error):
            raise error
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail):
            rel_dir = str(Path(directory).relative_to(self.store.config.output_dir))
            self._checked(rel_dir)
            if rel_dir not in parents and rel_dir != job_id:
                raise WebError("UNSAFE_DELETE", 409)
            directories.append(rel_dir)
            for name in dirs:
                self._checked(str(Path(directory, name).relative_to(self.store.config.output_dir)))
            for name in names:
                relative = str(Path(directory, name).relative_to(self.store.config.output_dir))
                path = self._checked(relative)
                if relative not in allowed or not path.is_file():
                    raise WebError("UNSAFE_DELETE", 409)
                actual.append((relative, path.stat().st_size))
        return actual, directories

    def purge(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise WebError("NOT_FOUND", 404)
        with self.store.lock:
            with self.store.connection() as db:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise WebError("NOT_FOUND", 404)
            if row["status"] in ACTIVE:
                raise WebError("JOB_ACTIVE", 409)
            if row["storage_state"] == "deleted":
                return {"status": "deleted", "deleted_bytes": 0}
            if self.store.downloads.get(job_id):
                raise WebError("FILE_IN_USE", 409)
            if row["purge_root"] and row["purge_root"] != str(self.store.config.output_dir):
                raise WebError("OUTPUT_CHANGED", 409)
            try:
                if row["storage_state"] != "present" and not row["purge_manifest"]:
                    raise ValueError
                manifest = json.loads(row["purge_manifest"]) if row["purge_manifest"] else None
            except (TypeError, ValueError):
                raise WebError("UNSAFE_DELETE", 409) from None
            try:
                actual, directories = self._inventory(row, manifest)
            except OSError:
                raise WebError("UNSAFE_DELETE", 409) from None
            if manifest is None:
                manifest = [p for p, _ in actual]
            with self.store.connection() as db:
                db.execute("UPDATE jobs SET storage_state='deleting',purge_root=?,purge_manifest=? WHERE id=?",
                           (str(self.store.config.output_dir), json.dumps(manifest), job_id))
            removed = 0
            try:
                for relative, _ in actual:
                    path = self._checked(relative)
                    try:
                        size = path.stat().st_size
                        path.unlink()
                        removed += size
                    except FileNotFoundError:
                        pass
                for relative in sorted(directories, key=lambda p: len(Path(p).parts), reverse=True):
                    path = self._checked(relative)
                    try:
                        path.rmdir()
                    except FileNotFoundError:
                        pass
            except (OSError, WebError):
                with self.store.connection() as db:
                    db.execute("UPDATE jobs SET storage_state='delete_failed' WHERE id=?", (job_id,))
                return {"status": "delete_failed", "deleted_bytes": removed, "reason": "DELETE_FAILED"}
            finally:
                self.invalidate()
            with self.store.connection() as db:
                db.execute("DELETE FROM files WHERE job_id=?", (job_id,))
                db.execute("UPDATE jobs SET storage_state='deleted',hidden_at=? WHERE id=?", (time.time(), job_id))
            self.invalidate()
            return {"status": "deleted", "deleted_bytes": removed}

    def recover_deletions(self):
        with self.store.connection() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE storage_state IN ('deleting','delete_failed')")]
        for job_id in ids:
            try:
                self.purge(job_id)
            except WebError:
                with self.store.connection() as db:
                    db.execute("UPDATE jobs SET storage_state='delete_failed' WHERE id=?", (job_id,))
                self.invalidate()
