import hashlib
import hmac
import secrets
import threading
from collections import deque
from time import monotonic as clock

from .config import WebError

COOKIE = "lumabox_session"


class Auth:
    def __init__(self, password: str):
        if not isinstance(password, str) or not 12 <= len(password) <= 4096:
            raise WebError("INVALID_PASSWORD")
        self.salt = secrets.token_bytes(16)
        self.verifier = self._hash(password)
        self.sessions = {}
        self.failures = deque()
        self.lock = threading.Lock()

    def _hash(self, password: str) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, 600000)

    def login(self, password: str) -> tuple[str, str]:
        with self.lock:
            now = clock()
            while self.failures and self.failures[0] <= now - 60:
                self.failures.popleft()
            if len(self.failures) >= 5:
                raise WebError("LOGIN_LIMIT", 429)
            if not isinstance(password, str) or not 1 <= len(password) <= 4096:
                raise WebError("INVALID_REQUEST")
            if not hmac.compare_digest(self.verifier, self._hash(password)):
                self.failures.append(now)
                raise WebError("LOGIN_FAILED", 401)
            self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > now}
            if len(self.sessions) >= 16:
                self.sessions.pop(next(iter(self.sessions)))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[token] = {"csrf": csrf, "expires": now + 28800}
            return token, csrf

    def session(self, token: str | None, csrf: str | None = None, *, write: bool = False) -> dict:
        with self.lock:
            session = self.sessions.get(token)
            if session is None or session["expires"] <= clock():
                self.sessions.pop(token, None)
                raise WebError("LOGIN_REQUIRED", 401)
            if write and (not csrf or not hmac.compare_digest(csrf, session["csrf"])):
                raise WebError("FORBIDDEN", 403)
            return dict(session)

    def logout(self, token: str):
        with self.lock:
            self.sessions.pop(token, None)
