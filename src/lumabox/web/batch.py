"""Bounded bulk actions over the existing persistent, serial job queue."""
from collections import Counter

from ..errors import LumaBoxError
from ..limits import DEFAULT_VIDEO_LIMIT_MIB, video_limit_bytes
from ..urls import canonical_page_url, extract_input_urls, parse_input
from .config import WebError
from .duplicates import DuplicateWorkError, find_match
from .store import ID

MAX_BATCH_ITEMS = 10


class BatchJobs:
    def __init__(self, store):
        self.store = store

    def _report(self, results):
        counts = dict(Counter(entry["status"] for entry in results))
        counts["total"] = len(results)
        with self.store.connection() as db:
            pending = db.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0]
        return {"results": results, "summary": counts, "pending_count": pending, "pending_limit": 10}

    def _failure(self, error, original=None):
        if isinstance(error, DuplicateWorkError):
            return {"status": "already_queued" if error.existing["match_type"] == "active" else "saved",
                    "existing": error.existing, "error": error.code}
        if error.code == "ALREADY_QUEUED" and original:
            with self.store.connection() as db:
                match = find_match(self.store, db, url=original["source_url"],
                                   work_id=original["work_id"], kind=original["kind"])
            if match:
                return {"status": "already_queued", "existing": match, "error": error.code}
        status = {"INVALID_INPUT": "invalid", "QUEUE_FULL": "queue_full", "ALREADY_QUEUED": "already_queued",
                  "NOT_FOUND": "not_found", "NOT_RETRYABLE": "not_retryable", "NOT_PENDING": "not_pending"}.get(error.code, "failed")
        return {"status": status, "error": error.code}

    def submit(self, text, video_limit_mib=DEFAULT_VIDEO_LIMIT_MIB, *, include_audio=False):
        if type(include_audio) is not bool:
            raise WebError("INVALID_REQUEST")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise WebError("INVALID_INPUT")
        try:
            video_limit_bytes(video_limit_mib)
        except LumaBoxError:
            raise WebError("INVALID_INPUT") from None
        links = extract_input_urls(text)
        if not links:
            raise WebError("INVALID_INPUT")
        if len(links) > MAX_BATCH_ITEMS:
            raise WebError("BATCH_LIMIT")
        results = []
        # Hold the same lock used by claim/cancel: accepted links retain paste
        # order, and one worker cannot take a slot halfway through this batch.
        with self.store.lock:
            for index, link in enumerate(links, 1):
                entry = {"index": index, "url": None}
                try:
                    entry["url"] = canonical_page_url(parse_input(link))
                    job = self.store.create(link, video_limit_mib, include_audio=include_audio,
                                            supplement_duplicate_audio=False)
                    entry.update(status="queued", job=job)
                except LumaBoxError:
                    entry.update(status="invalid", error="INVALID_INPUT")
                except WebError as error:
                    entry.update(self._failure(error))
                results.append(entry)
            return self._report(results)

    @staticmethod
    def _ids(ids):
        if not isinstance(ids, list) or not ids:
            raise WebError("INVALID_REQUEST")
        if len(ids) > MAX_BATCH_ITEMS:
            raise WebError("BATCH_LIMIT")
        if any(not isinstance(value, str) or not ID.fullmatch(value) for value in ids) or len(set(ids)) != len(ids):
            raise WebError("INVALID_REQUEST")
        return ids

    def cancel(self, ids):
        ids = self._ids(ids)
        results = []
        with self.store.lock:
            for index, job_id in enumerate(ids, 1):
                entry = {"index": index, "id": job_id}
                try:
                    job = self.store.get(job_id)
                    if job["status"] not in {"pending", "cancelled"}:
                        raise WebError("NOT_PENDING", 409)
                    entry.update(status="cancelled", job=self.store.cancel(job_id))
                except WebError as error:
                    entry.update(self._failure(error))
                results.append(entry)
            return self._report(results)

    def retry(self, ids):
        ids = self._ids(ids)
        results = []
        with self.store.lock:
            for index, job_id in enumerate(ids, 1):
                entry = {"index": index, "id": job_id, "url": None}
                original = None
                try:
                    original = self.store.get(job_id)
                    entry["url"] = original["source_url"]
                    if original["status"] not in {"failed", "cancelled", "expired"}:
                        raise WebError("NOT_RETRYABLE", 409)
                    job = self.store.retry(job_id, original["video_limit_mib"], supplement_duplicate_audio=False)
                    entry.update(status="queued", job=job)
                except WebError as error:
                    entry.update(self._failure(error, original))
                results.append(entry)
            return self._report(results)
