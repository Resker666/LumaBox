import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from .auth import Auth, COOKIE
from .config import WebConfig, WebError
from .store import JobStore
from .worker import JobWorker

MESSAGES = {
    "LOGIN_REQUIRED": "请先登录光匣。", "LOGIN_FAILED": "密码不正确，请重试。",
    "LOGIN_LIMIT": "尝试次数过多，请一分钟后再试。", "FORBIDDEN": "请求校验失败，请刷新页面重试。",
    "INVALID_REQUEST": "请求格式不正确。", "BODY_TOO_LARGE": "提交内容过长，请缩短后重试。",
    "JSON_REQUIRED": "请提交 JSON 格式的请求。", "INVALID_INPUT": "请粘贴一条抖音视频或图集链接。",
    "AUTHORIZATION_REQUIRED": "请先确认你有权下载此作品。", "QUEUE_FULL": "等待队列已满，请稍后再试。",
    "NOT_FOUND": "任务或文件不存在，文件可能已过期。", "SERVER_ERROR": "暂时无法完成请求，请稍后重试。",
}


def error_response(error: WebError):
    return JSONResponse({"error": error.code, "message": MESSAGES.get(error.code, MESSAGES["SERVER_ERROR"])}, status_code=error.status)


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
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                ]
            await send(message)

        try:
            headers = dict(scope["headers"])
            hosts = [value for key, value in scope["headers"] if key == b"host"]
            host = headers.get(b"host", b"").decode("latin-1")
            if len(hosts) != 1 or host not in self.hosts:
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


def create_app(config: WebConfig, password: str, worker: JobWorker | None = None) -> FastAPI:
    auth = Auth(password)
    store = worker.store if worker is not None else JobStore(config)
    worker = worker if worker is not None else JobWorker(config, store)

    @asynccontextmanager
    async def lifespan(app):
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(LocalBoundary, port=config.port)

    @app.exception_handler(WebError)
    async def web_error(request, exc):
        return error_response(exc)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return error_response(WebError("SERVER_ERROR", 500))

    def session(request, write=False):
        return auth.session(request.cookies.get(COOKIE), request.headers.get("x-csrf-token"), write=write)

    @app.post("/api/login")
    async def login(request: Request):
        body = await object_body(request)
        if set(body) != {"password"}:
            raise WebError("INVALID_REQUEST")
        token, csrf = auth.login(body["password"])
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(COOKIE, token, max_age=28800, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/session")
    def get_session(request: Request):
        return {"csrf": session(request)["csrf"], "retention_hours": 24, "pending_limit": 10}

    @app.post("/api/logout")
    def logout(request: Request):
        session(request, write=True)
        auth.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"status": "ok"})
        response.delete_cookie(COOKIE, httponly=True, samesite="strict")
        return response

    @app.post("/api/jobs", status_code=202)
    async def submit_job(request: Request):
        session(request, write=True)
        if worker.stopping.is_set():
            raise WebError("SERVER_ERROR", 503)
        body = await object_body(request)
        if set(body) != {"input", "authorized"}:
            raise WebError("INVALID_REQUEST")
        if body["authorized"] is not True:
            raise WebError("AUTHORIZATION_REQUIRED")
        return store.create(body["input"])

    @app.get("/api/jobs")
    def list_jobs(request: Request):
        session(request)
        return {"jobs": store.list()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        session(request)
        return store.get(job_id)

    @app.get("/api/files/{file_id}")
    def download(file_id: str, request: Request):
        session(request)
        file = store.file(file_id)
        try:
            stream = store.file_path(file).open("rb")
        except OSError:
            raise WebError("NOT_FOUND", 404) from None

        def chunks():
            try:
                while chunk := stream.read(64 * 1024):
                    yield chunk
            finally:
                stream.close()

        return StreamingResponse(chunks(), media_type=file["mime"], headers={
            "Content-Length": str(file["bytes"]), "Content-Disposition": f'attachment; filename="{file["name"]}"'},
            background=BackgroundTask(stream.close))

    static = Path(__file__).parent / "static"

    @app.get("/")
    def index():
        return FileResponse(static / "index.html", media_type="text/html")

    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")
    return app
