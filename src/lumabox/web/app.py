import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from .. import __version__
from .config import WebConfig, WebError
from .folders import open_folder
from .media import preview_response, LeasedResponse
from .store import JobStore
from .worker import JobWorker
from .access import CloudAccess, AccessBoundary, COOKIE
from .duplicates import DuplicateWorkError
from .batch import BatchJobs, MAX_BATCH_ITEMS

MESSAGES = {
    "BATCH_LIMIT": "一次最多处理 10 条链接或任务，请分批提交。",
    "AUDIO_UNAVAILABLE": "请先完成视频下载，再提取音频；本版暂不支持图集配乐。",
    "FILE_IN_USE": "文件正在下载或预览，请停止预览或等待传输完成后再删除。",
    "UNSAFE_DELETE": "目录包含无法确认归属的内容或链接，未执行删除，请检查保存位置。",
    "OUTPUT_CHANGED": "此删除任务属于旧保存目录，请切回原目录后处理。",
    "DELETE_FAILED": "文件尚未全部删除，请检查权限后在文件管理中重试。",
    "ALREADY_DOWNLOADING": "此作品已在队列中，请查看已有任务。",
    "DUPLICATE_WORK": "此作品已经保存，可查看已有收藏或选择重新下载。",
    "RESULT_UNAVAILABLE": "已保存的文件不可用，可重新下载此作品。",
    "FORBIDDEN": "请求校验失败，请刷新页面重试。",
    "INVALID_REQUEST": "请求格式不正确。", "BODY_TOO_LARGE": "提交内容过长，请缩短后重试。",
    "JSON_REQUIRED": "请提交 JSON 格式的请求。", "INVALID_INPUT": "请粘贴一条抖音视频或图集链接。",
    "QUEUE_FULL": "等待队列已满，请稍后再试。", "ALREADY_QUEUED": "此任务已有重试正在等待或运行。",
    "NOT_RETRYABLE": "此任务当前无需重试。", "FOLDER_OPEN_UNAVAILABLE": "暂时无法打开文件夹，可按保存位置查找或另存文件。",
    "NOT_CANCELLABLE": "任务已经结束，无需取消。请刷新查看最新状态。",
    "NOT_FOUND": "任务或文件不存在，文件可能已过期。", "SERVER_ERROR": "暂时无法完成请求，请稍后重试。",
    "JOB_ACTIVE": "任务仍在等待或运行，请完成后再移除记录。", "PREVIEW_UNAVAILABLE": "此文件暂不支持预览。",
}


def error_response(error: WebError):
    body = {"error": error.code, "message": MESSAGES.get(error.code, MESSAGES["SERVER_ERROR"])}
    if isinstance(error, DuplicateWorkError):
        body["existing"] = error.existing
    return JSONResponse(body, status_code=error.status)


class LocalBoundary:
    """Bound request bodies before parsing; enforce local Host and same-origin writes."""
    def __init__(self, app, port):
        self.app = app
        suffix = "" if port == 80 else f":{port}"
        self.hosts = {"127.0.0.1" + suffix, "localhost" + suffix}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                ]
            await send(message)

        try:
            headers = dict(scope["headers"])
            hosts = [value for key, value in scope["headers"] if key == b"host"]
            host = headers.get(b"host", b"").decode("latin-1")
            if len(hosts) != 1 or host not in self.hosts:
                raise WebError("FORBIDDEN", 403)
            if scope["path"].startswith("/api/") and headers.get(b"sec-fetch-site") == b"cross-site":
                raise WebError("FORBIDDEN", 403)
            if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                if headers.get(b"origin", b"").decode("latin-1") != "http://" + host:
                    raise WebError("FORBIDDEN", 403)
                if headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json":
                    raise WebError("JSON_REQUIRED", 415)
                try:
                    if int(headers.get(b"content-length", b"0")) > 8192:
                        raise WebError("BODY_TOO_LARGE", 413)
                except ValueError:
                    raise WebError("INVALID_REQUEST") from None
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        raise WebError("INVALID_REQUEST")
                    body.extend(message.get("body", b""))
                    if len(body) > 8192:
                        raise WebError("BODY_TOO_LARGE", 413)
                    if not message.get("more_body", False):
                        break
                original_receive = receive
                delivered = False

                async def bounded_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = bounded_receive
            await self.app(scope, receive, secure_send)
        except WebError as exc:
            await error_response(exc)(scope, receive, secure_send)


async def object_body(request: Request) -> dict:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (ValueError, UnicodeError):
        raise WebError("INVALID_REQUEST") from None


def create_app(config: WebConfig, worker: JobWorker | None = None) -> FastAPI:
    access = CloudAccess(Path(config.access_file)) if config.access_file is not None else None
    csrf = secrets.token_urlsafe(32)
    store = worker.store if worker is not None else JobStore(config)
    worker = worker if worker is not None else JobWorker(config, store)
    batches = BatchJobs(store)

    @asynccontextmanager
    async def lifespan(app):
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    if access is not None:
        app.add_middleware(AccessBoundary, access=access)
    app.add_middleware(LocalBoundary, port=config.port)

    @app.exception_handler(WebError)
    async def web_error(request, exc):
        return error_response(exc)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return error_response(WebError("SERVER_ERROR", 500))

    def write_guard(request):
        token = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(token.encode("utf-8"), csrf.encode("ascii")):
            raise WebError("FORBIDDEN", 403)

    @app.get("/api/session")
    def get_session():
        return {"csrf": csrf, "application": "lumabox", "version": __version__,
                "remote_mode": config.remote_mode,
                "access_protected": access is not None,
                "data_directory": str(config.data_dir),
                "video_limit_mib": config.video_limit_mib, "max_video_limit_mib": 8192,
                "output_directory": str(config.output_dir), "pending_limit": 10, "persistent": True,
                "batch_limit": MAX_BATCH_ITEMS,
                "image_limit_mib": 20, "gallery_limit_mib": 200, "gallery_count_limit": 50}

    @app.post("/api/jobs", status_code=202)
    async def submit_job(request: Request):
        write_guard(request)
        if worker.stopping.is_set():
            raise WebError("SERVER_ERROR", 503)
        body = await object_body(request)
        if "input" not in body or set(body) - {"input", "video_limit_mib", "force_download", "include_audio"}:
            raise WebError("INVALID_REQUEST")
        return store.create(body["input"], body.get("video_limit_mib", config.video_limit_mib), force_download=body.get("force_download", False), include_audio=body.get("include_audio", False))

    @app.post("/api/jobs/batch", status_code=202)
    async def submit_batch(request: Request):
        write_guard(request)
        if worker.stopping.is_set():
            raise WebError("SERVER_ERROR", 503)
        body = await object_body(request)
        if "input" not in body or set(body) - {"input", "video_limit_mib", "include_audio"}:
            raise WebError("INVALID_REQUEST")
        return batches.submit(body["input"], body.get("video_limit_mib", config.video_limit_mib),
                              include_audio=body.get("include_audio", False))

    @app.post("/api/jobs/batch/cancel", status_code=202)
    async def cancel_batch(request: Request):
        write_guard(request)
        body = await object_body(request)
        if set(body) != {"ids"}:
            raise WebError("INVALID_REQUEST")
        return batches.cancel(body["ids"])

    @app.post("/api/jobs/batch/retry", status_code=202)
    async def retry_batch(request: Request):
        write_guard(request)
        if worker.stopping.is_set():
            raise WebError("SERVER_ERROR", 503)
        body = await object_body(request)
        if set(body) != {"ids"}:
            raise WebError("INVALID_REQUEST")
        return batches.retry(body["ids"])

    @app.post("/api/jobs/{job_id}/audio", status_code=202)
    async def extract_job_audio(job_id: str, request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        if worker.stopping.is_set():
            raise WebError("SERVER_ERROR", 503)
        return store.audio.request(job_id)

    @app.post("/api/jobs/{job_id}/audio/cancel")
    async def cancel_job_audio(job_id: str, request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        return store.audio.cancel(job_id)

    @app.get("/api/storage")
    def storage_summary(request: Request):
        query = request.query_params
        if set(query) - {"refresh"} or query.get("refresh", "0") not in {"0", "1"}:
            raise WebError("INVALID_REQUEST")
        return store.storage.summary(refresh=query.get("refresh") == "1")

    @app.get("/api/storage/items")
    def storage_items(request: Request):
        query = request.query_params
        try:
            if set(query) - {"scope", "page", "page_size"}:
                raise ValueError
            page, size = int(query.get("page", "1")), int(query.get("page_size", "20"))
        except ValueError:
            raise WebError("INVALID_REQUEST") from None
        return store.storage.items(scope=query.get("scope", "all"), page=page, page_size=size)

    @app.post("/api/jobs/{job_id}/restore")
    async def restore_record(job_id: str, request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        return store.restore(job_id)

    @app.post("/api/jobs/{job_id}/purge")
    async def purge_record(job_id: str, request: Request):
        write_guard(request)
        body = await object_body(request)
        if set(body) != {"confirm"} or body.get("confirm") is not True:
            raise WebError("INVALID_REQUEST")
        result = store.storage.purge(job_id)
        if result["status"] != "deleted":
            result["message"] = MESSAGES["DELETE_FAILED"]
        return result

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str, request: Request):
        write_guard(request)
        if worker.stopping.is_set():
            raise WebError("SERVER_ERROR", 503)
        body = await object_body(request)
        if set(body) - {"video_limit_mib"}:
            raise WebError("INVALID_REQUEST")
        return store.retry(job_id, body.get("video_limit_mib", config.video_limit_mib))

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        return store.cancel(job_id)

    @app.post("/api/jobs/{job_id}/open-folder")
    async def show_folder(job_id: str, request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        open_folder(store.folder(job_id))
        return {"status": "ok"}

    @app.get("/api/jobs")
    def list_jobs(request: Request):
        query = request.query_params
        try:
            page, page_size = int(query.get("page", "1")), int(query.get("page_size", "20"))
        except ValueError:
            raise WebError("INVALID_REQUEST") from None
        return store.search(query.get("q", ""), query.get("status", "all"), page, page_size, scope=query.get("scope", "all"))

    @app.get("/api/jobs/active")
    def active_jobs():
        return {"jobs": store.active()}

    @app.post("/api/jobs/clear-failed")
    async def clear_failed(request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        return {"removed": store.clear_failed()}

    @app.post("/api/jobs/{job_id}/remove")
    async def remove_record(job_id: str, request: Request):
        write_guard(request)
        if await object_body(request):
            raise WebError("INVALID_REQUEST")
        return {"removed": store.remove(job_id)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        return store.get(job_id)

    @app.api_route("/api/files/{file_id}/preview", methods=["GET", "HEAD"])
    def preview(file_id: str, request: Request):
        return preview_response(store, file_id, request)

    @app.get("/api/files/{file_id}")
    def download(file_id: str, request: Request):
        lease = store.open_file(file_id)
        file, stream = lease.__enter__()
        released, release_lock = False, threading.Lock()

        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    lease.__exit__(None, None, None)

        def chunks():
            try:
                while chunk := stream.read(64 * 1024):
                    yield chunk
            finally:
                release()

        return LeasedResponse(chunks(), release=release, media_type=file["mime"], headers={
            "Content-Length": str(file["bytes"]), "Content-Disposition": f'attachment; filename="{file["name"]}"'},
        )

    static = Path(__file__).parent / "static"

    if access is not None:
        @app.get("/login")
        def login_page():
            return FileResponse(static / "login.html", media_type="text/html")

        @app.post("/auth/login")
        async def login(request: Request):
            body = await object_body(request)
            password, remember = body.get("password"), body.get("remember", False)
            if (set(body) - {"password", "remember"} or not isinstance(password, str)
                    or not 1 <= len(password) <= 256 or type(remember) is not bool):
                raise WebError("INVALID_REQUEST")
            try:
                password.encode("utf-8")
            except UnicodeError:
                raise WebError("INVALID_REQUEST") from None
            token = access.login(password, remember, request.cookies.get(COOKIE))
            if not isinstance(token, str):
                return token
            response = JSONResponse({"status": "ok"})
            response.set_cookie(COOKIE, token, max_age=604800 if remember else None,
                                httponly=True, samesite="strict", secure=config.secure_cookie)
            return response

        @app.post("/auth/logout")
        async def logout(request: Request):
            write_guard(request)
            if await object_body(request):
                raise WebError("INVALID_REQUEST")
            access.logout(request.cookies.get(COOKIE))
            response = JSONResponse({"status": "ok"})
            response.delete_cookie(COOKIE, httponly=True, samesite="strict", secure=config.secure_cookie)
            return response

    @app.get("/")
    def index():
        return FileResponse(static / "index.html", media_type="text/html")

    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")
    return app
