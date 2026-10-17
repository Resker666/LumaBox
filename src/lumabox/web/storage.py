"""Current-output inventory; disk free space is not a LumaBox quota."""
import os
from pathlib import Path
import shutil
import threading
import time

from ..metadata import normalize_title
from .config import WebError, is_link


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
