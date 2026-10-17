"""Match only a confirmed identity or normalized link, with readable results."""
from ..metadata import normalize_title
from .config import WebError

ACTIVE = frozenset({"pending", "running", "retry_wait", "cancelling"})


class DuplicateWorkError(WebError):
    def __init__(self, existing):
        super().__init__("ALREADY_DOWNLOADING" if existing["match_type"] == "active" else "DUPLICATE_WORK", 409)
        self.existing = existing


def available(store, db, job_id):
    files = db.execute("SELECT * FROM files WHERE job_id=? AND mime<>'audio/mp4'", (job_id,)).fetchall()
    if not files:
        return False
    try:
        for file in files:
            with store.file_path(file).open("rb"):
                pass
    except (OSError, WebError):
        return False
    return True


def find_match(store, db, *, url, work_id, kind, exclude_id=None, force_download=False):
    rows = db.execute("""SELECT * FROM jobs WHERE storage_state='present' AND id<>?
        AND (url=? OR (work_id=? AND kind=?))
        AND status IN ('pending','running','retry_wait','cancelling','success')
        ORDER BY CASE WHEN status='success' THEN 1 ELSE 0 END, created_at, rowid""",
        (exclude_id or "", url, work_id, kind)).fetchall()
    current = db.execute("SELECT created_at FROM jobs WHERE id=?", (exclude_id,)).fetchone() if exclude_id else None
    for row in rows:
        active = row["status"] in ACTIVE
        if active and (row["hidden_at"] is not None or (current and row["created_at"] > current["created_at"])):
            continue
        if not active and (force_download or not available(store, db, row["id"])):
            continue
        return {"id": row["id"], "match_type": "active" if active else "saved",
                "hidden": row["hidden_at"] is not None, "work_id": row["work_id"], "kind": row["kind"],
                "title": normalize_title(row["title"])}
    return None
