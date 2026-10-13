import asyncio
import re
import time
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError

from .errors import LumaBoxError, check_status
from .extract import DOM_SNAPSHOT, detect_access, extract_work
from .net import resolve_public
from .proxy import PublicProxy
from .urls import validate_page_url, page_identity, validate_media_url

RESOURCE_WAIT = 45.0
MEDIA_TYPES = frozenset({"video/mp4", "video/webm", "video/mp2t", "audio/mp4", "audio/mpeg", "audio/aac",
                        "application/dash+xml", "application/vnd.apple.mpegurl", "application/x-mpegurl"})
MEDIA_EXTENSIONS = frozenset({"mp4", "webm", "m4s", "m4a", "aac", "ts", "mpd", "m3u8"})


def _diagnostic_host(url):
    try:
        host = urlsplit(url).hostname
        return host if host and re.fullmatch(r"[a-z0-9.-]{1,253}", host) else None
    except ValueError:
        return None


class NavigationGuard:
    def __init__(self):
        self.depths = {}

    def check(self, url: str, redirected_from: str | None, request_id: str | None = None):
        validate_page_url(url)
        depth = self.depths.get(redirected_from, 0) + 1 if redirected_from is not None else 0
        if depth > 5:
            raise LumaBoxError("UNSAFE_TARGET", "Navigation exceeded five redirects")
        self.depths[request_id if request_id is not None else str(len(self.depths))] = depth


class BrowserSession:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.error = None
        self.http_status = None
        self.guard = NavigationGuard()
        self.stack = AsyncExitStack()
        self.main_frame_id = None
        self.navigation_hosts = set()
        self.blocked_documents = []
        self.media_responses = []
        self.media_response_count = 0
        self.media_response_truncated = False
        self.diagnostics = {"stage": "browser_start"}

    async def __aenter__(self):
        try:
            self.proxy = await self.stack.enter_async_context(PublicProxy())
            p = await self.stack.enter_async_context(async_playwright())
            self.browser = await p.chromium.launch(headless=self.headless, proxy={"server": self.proxy.server_url}, args=[
                "--proxy-bypass-list=<-loopback>", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            self.stack.push_async_callback(self.browser.close)
            self.context = await self.browser.new_context(service_workers="block")
            self.stack.push_async_callback(self.context.close)
            self.page = await self.context.new_page()
            self.context.on("page", self._close_extra_page)
            self.page.on("response", self._response)
            cdp = await self.context.new_cdp_session(self.page)
            self.cdp = cdp
            self.main_frame_id = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            cdp.on("Fetch.requestPaused", self._document)
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]})
            return self
        except BaseException as exc:
            await self.stack.aclose()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise LumaBoxError("DEPENDENCY_MISSING", "Chromium could not start; check the browser installation") from None

    async def __aexit__(self, *args):
        await self.stack.aclose()

    async def _close_extra_page(self, page):
        if page != self.page:
            await page.close()

    async def _document(self, event):
        is_main = event.get("frameId") == self.main_frame_id
        host = _diagnostic_host(event["request"]["url"])
        try:
            self.guard.check(event["request"]["url"], event.get("redirectedRequestId"), event["requestId"])
            if is_main:
                self.navigation_hosts.add(host)
            await self.cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except LumaBoxError as exc:
            detail = {"stage": "page_navigation", "reason": exc.message,
                      "host": host, "frame": "main" if is_main else "child"}
            if len(self.blocked_documents) < 10:
                self.blocked_documents.append(detail)
            # A blocked embedded document remains blocked. Only a rejected main
            # navigation invalidates the target work's entire browsing session.
            if is_main:
                self.error = exc
                self.diagnostics = detail.copy()
            await self.cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
        except PlaywrightError:
            # Context shutdown can cancel paused requests.
            pass

    def _response(self, response):
        if response.request.frame != self.page.main_frame:
            return
        if response.request.is_navigation_request():
            self.http_status = response.status
            try:
                check_status(response.status)
            except LumaBoxError as exc:
                self.error = exc
        self._observe_media_response(response)

    def _observe_media_response(self, response):
        resource_type = response.request.resource_type
        if resource_type not in {"media", "fetch", "xhr"}:
            return
        host = _diagnostic_host(response.url)
        if not host:
            return
        headers = response.headers
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        is_media = (content_type in MEDIA_TYPES or content_type.startswith(("video/", "audio/")) or resource_type == "media")
        if not is_media and content_type == "application/octet-stream":
            try:
                validate_media_url(response.url)
                is_media = True
            except LumaBoxError:
                pass
        if not is_media:
            return
        safe_type = content_type if content_type in MEDIA_TYPES else (
            "video/other" if content_type.startswith("video/") else
            "audio/other" if content_type.startswith("audio/") else
            "application/octet-stream" if content_type == "application/octet-stream" else "unknown")
        extension = urlsplit(response.url).path.rsplit(".", 1)[-1].lower()
        hint = extension if extension in MEDIA_EXTENSIONS else "unknown"
        length = headers.get("content-length", "")
        sample_bytes = int(length) if re.fullmatch(r"[0-9]{1,20}", length) else None
        byte_range = re.fullmatch(r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20}|\*)", headers.get("content-range", ""))
        total_bytes = None
        if byte_range and byte_range[3] != "*":
            start, end, total = map(int, byte_range.groups())
            if 0 <= start <= end < total:
                total_bytes = total
        # These describe normal page responses, not verified target resources.
        # Only bounded, sanitized metadata is retained; no URLs or bodies.
        item = {"host": host, "resource_type": resource_type, "status": response.status,
                "content_type": safe_type, "format_hint": hint, "ranged": response.status == 206 or bool(byte_range),
                "total_bytes": total_bytes, "sample_bytes": sample_bytes, "count": 1}
        self.media_response_count += 1
        keys = ("host", "resource_type", "status", "content_type", "format_hint", "ranged", "total_bytes")
        for existing in self.media_responses:
            if all(existing[key] == item[key] for key in keys):
                existing["count"] += 1
                return
        if len(self.media_responses) < 20:
            self.media_responses.append(item)
        else:
            self.media_response_truncated = True

    def _check_errors(self, initial_host, navigation_failed=False):
        if self.error:
            raise self.error
        if navigation_failed:
            for failure in self.proxy.failures:
                if failure["host"] in self.navigation_hosts | {initial_host}:
                    self.diagnostics = {"stage": "page_navigation", "reason": "Browser network connection failed",
                                        "host": failure["host"]}
                    raise LumaBoxError(failure["code"], "Browser network connection failed")

    async def load(self, link):
        self.diagnostics = {"stage": "input_policy"}
        validate_page_url(link.url)
        host = urlsplit(link.url).hostname
        self.diagnostics = {"stage": "dns_resolution", "host": host}
        try:
            await asyncio.to_thread(resolve_public, host)
        except LumaBoxError as exc:
            self.diagnostics["reason"] = exc.message
            raise
        self.diagnostics = {"stage": "page_navigation", "host": host}
        try:
            await self.page.goto(link.url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightError:
            self._check_errors(host, navigation_failed=True)
            raise LumaBoxError("RESOURCE_TIMEOUT", "The work page could not load in 30 seconds") from None
        deadline = time.monotonic() + RESOURCE_WAIT
        last = None
        expected_identity = (link.work_id, link.kind) if link.work_id else None
        while time.monotonic() < deadline:
            self._check_errors(host)
            self.diagnostics = {"stage": "target_identity"}
            identity = page_identity(self.page.url)
            if identity:
                work_id, kind = identity
                if expected_identity is None:
                    expected_identity = identity
                if identity != expected_identity:
                    raise LumaBoxError("UNSUPPORTED_MEDIA", "The browser switched to a different work")
                self.diagnostics = {"stage": "media_extraction"}
                snapshot = await self.page.evaluate(DOM_SNAPSHOT, {"work_id": work_id, "kind": kind})
                self._check_errors(host)
                self.diagnostics["resource_hosts"] = sorted({host for r in snapshot["resources"]
                    if (host := _diagnostic_host(r.get("url", "")))})
                self.diagnostics["scope_confirmed"] = bool(snapshot.get("scope_confirmed"))
                self.diagnostics["identity_source"] = snapshot.get("identity_source")
                self.diagnostics["media_schemes"] = sorted({
                    scheme if scheme in {"https", "blob"} else "other"
                    for r in snapshot["resources"] if r.get("url")
                    for scheme in [r["url"].partition(":")[0]]})
                # A scoped blob player is media that may still expose a normal
                # HTTPS source; a comment or promotion login is not a view gate.
                has_media = snapshot.get("scope_confirmed") and any(r.get("url") for r in snapshot["resources"])
                detect_access(snapshot["text"], has_media)
                try:
                    return extract_work(snapshot, work_id)
                except LumaBoxError as exc:
                    if exc.code != "UNSUPPORTED_MEDIA":
                        self.diagnostics["reason"] = exc.message
                        raise
                    last = exc
            else:
                text = await self.page.locator("body").inner_text(timeout=1000)
                detect_access(text[:20000], False)
            await asyncio.sleep(0.25)
        if last:
            self.diagnostics["reason"] = last.message
            raise last
        raise LumaBoxError("RESOURCE_TIMEOUT", "Target resources were not available in 45 seconds")

    async def media_headers(self, url: str) -> dict[str, str]:
        validate_media_url(url)
        cookies = await self.context.cookies([url])
        headers = {"User-Agent": await self.page.evaluate("navigator.userAgent"), "Referer": self.page.url, "Accept-Encoding": "identity"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return headers
