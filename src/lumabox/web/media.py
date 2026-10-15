"""Inline previews of published media, with bounded byte ranges and file leases."""
import re
import threading

from starlette.responses import Response, StreamingResponse

from .config import WebError


class LeasedResponse(StreamingResponse):
    def __init__(self, *args, release, **kwargs):
        self.release = release
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also release on send errors/disconnect before iteration begins.
            self.release()


def preview_response(store, file_id: str, request):
    lease = store.open_file(file_id)
    file, stream = lease.__enter__()
    released, lock = False, threading.Lock()

    def release():
        nonlocal released
        with lock:
            if not released:
                released = True
                lease.__exit__(None, None, None)

    try:
        if file["mime"] not in ("video/mp4", "image/jpeg", "image/webp"):
            raise WebError("PREVIEW_UNAVAILABLE", 415)
        size = file["bytes"]
        start, end, status = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{file["name"]}"'}
        requested = request.headers.get("range")
        if requested is not None:
            match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested.strip())
            valid = bool(match and (match[1] or match[2]))
            if valid:
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    suffix = int(match[2])
                    start = max(0, size - suffix)
                    valid = suffix > 0
                valid = valid and 0 <= start <= end < size
            if not valid:
                release()
                return Response(status_code=416, headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        count = max(0, end - start + 1)
        headers["Content-Length"] = str(count)
        if request.method == "HEAD":
            release()
            return Response(status_code=status, headers=headers, media_type=file["mime"])
        stream.seek(start)

        def chunks():
            remaining = count
            try:
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
            finally:
                release()

        return LeasedResponse(chunks(), release=release, status_code=status, media_type=file["mime"], headers=headers)
    except BaseException:
        release()
        raise
