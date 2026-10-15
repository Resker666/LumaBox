"""Optional single-owner cloud access; local installations remain open on loopback."""
import hashlib
import hmac
import json
import secrets
import threading
from collections import deque
from time import monotonic

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .config import WebError, is_link

COOKIE = "lumabox_session"


class CloudAccess:
    def __init__(self, path):
        try:
            if is_link(path) or path.stat().st_size > 8192:
                raise ValueError
            password = json.loads(path.read_bytes())["password"]
            if not isinstance(password, str) or not 1 <= len(password) <= 256:
                raise ValueError
            password.encode("utf-8")
        except (OSError, ValueError, KeyError, TypeError):
            raise WebError("INVALID_ACCESS_CONFIG") from None
        self.salt = secrets.token_bytes(16)
        self.digest = self._digest(password)
        self.sessions = {}
        self.failures = deque(maxlen=5)
        self.lock = threading.Lock()

    def _digest(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, 200_000)

    def _prune(self, now):
        self.sessions = {key: end for key, end in self.sessions.items() if end > now}
        while self.failures and self.failures[0] <= now - 60:
            self.failures.popleft()

    def authenticated(self, token):
        if not token or len(token) > 128:
            return False
        key = hashlib.sha256(token.encode("utf-8")).digest()
        with self.lock:
            return self.sessions.get(key, 0) > monotonic()

    def login(self, password, remember, previous):
        with self.lock:
            now = monotonic()
            self._prune(now)
            if len(self.failures) >= 5:
                retry = max(1, int(61 - (now - self.failures[0])))
                return JSONResponse({"error": "LOGIN_RATE_LIMITED", "message": f"尝试次数较多，请 {retry} 秒后再试。"}, status_code=429, headers={"Retry-After": str(retry)})
            if not hmac.compare_digest(self._digest(password), self.digest):
                self.failures.append(now)
                return JSONResponse({"error": "INVALID_PASSWORD", "message": "密码不正确，请检查后重试。"}, status_code=401)
            self.failures.clear()
            self._revoke(previous)
            if len(self.sessions) >= 100:
                del self.sessions[min(self.sessions, key=self.sessions.get)]
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).digest()] = now + (604800 if remember else 28800)
            return token

    def _revoke(self, token):
        if token and len(token) <= 128:
            self.sessions.pop(hashlib.sha256(token.encode("utf-8")).digest(), None)

    def logout(self, token):
        with self.lock:
            self._revoke(token)


class AccessBoundary:
    def __init__(self, app, access):
        self.app, self.access = app, access

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        signed_in = self.access.authenticated(request.cookies.get(COOKIE))
        path, method = scope["path"], scope["method"]
        public = method in ("GET", "HEAD") and path in ("/login", "/static/app.css", "/static/login.css", "/static/login.js")
        public = public or (method == "POST" and path == "/auth/login")
        if signed_in and path == "/login" and method in ("GET", "HEAD"):
            response = RedirectResponse("/", status_code=303)
        elif signed_in or public:
            await self.app(scope, receive, send)
            return
        elif path.startswith(("/api/", "/auth/")):
            response = JSONResponse({"error": "LOGIN_REQUIRED", "message": "登录已过期，请重新登录。"}, status_code=401)
        else:
            response = RedirectResponse("/login", status_code=303)
        await response(scope, receive, send)
