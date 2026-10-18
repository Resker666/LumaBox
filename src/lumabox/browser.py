import asyncio
import base64
import json
import re
import time
from contextlib import AsyncExitStack
from dataclasses import replace
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError

from .errors import LumaBoxError, check_status
from .extract import DOM_SNAPSHOT, detect_access, extract_work, extract_gallery_music
from .net import resolve_public
from .metadata import normalize_title
from .proxy import PublicProxy
from .urls import validate_page_url, page_identity, validate_media_url, MEDIA_HOSTS
from .video_response import DETAIL_BODY_LIMIT, detail_request_id, extract_detail_video

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
        self.expected_identity = None
        self.confirmed_work = None
        self.detail_work = None
        self.detail_error = None
        self.detail_tasks = set()
        self.detail_pending = {}
        self.detail_response_count = 0
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
            cdp.on("Network.responseReceived", self._observe_detail_response)
            cdp.on("Network.dataReceived", self._detail_data)
            cdp.on("Network.loadingFinished", self._detail_finished)
            cdp.on("Network.loadingFailed", self._detail_failed)
            await cdp.send("Network.enable", {"maxResourceBufferSize": DETAIL_BODY_LIMIT,
                "maxTotalBufferSize": 4 * DETAIL_BODY_LIMIT, "maxPostDataSize": 0})
            cdp.on("Fetch.requestPaused", self._document)
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]})
            return self
        except BaseException as exc:
            await self.stack.aclose()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise LumaBoxError("DEPENDENCY_MISSING", "Chromium could not start; check the browser installation") from None

    async def __aexit__(self, *args):
        for task in tuple(self.detail_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.detail_tasks), return_exceptions=True)
        self.detail_pending.clear()
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

    def _observe_detail_response(self, event):
        if event.get("frameId") != self.main_frame_id or event.get("type") not in {"Fetch", "XHR"}:
            return
        response = event["response"]
        work_id = detail_request_id(response["url"])
        if not work_id:
            return
        try:
            identity = self.expected_identity or page_identity(self.page.url)
        except LumaBoxError:
            return
        if identity != (work_id, "video"):
            return
        try:
            check_status(response["status"])
        except LumaBoxError as exc:
            self.error = exc
            return
        if response["status"] != 200 or response.get("mimeType", "").lower() != "application/json" or self.detail_response_count >= 4:
            return
        self.detail_response_count += 1
        headers = {key.lower(): str(value) for key, value in response.get("headers", {}).items()}
        length = headers.get("content-length", "")
        if length and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > DETAIL_BODY_LIMIT):
            return
        self.detail_pending[event["requestId"]] = {"work_id": work_id, "bytes": 0}

    def _detail_data(self, event):
        pending = self.detail_pending.get(event["requestId"])
        if pending:
            pending["bytes"] += event["dataLength"]
            if pending["bytes"] > DETAIL_BODY_LIMIT:
                self.detail_pending.pop(event["requestId"], None)

    def _detail_failed(self, event):
        self.detail_pending.pop(event["requestId"], None)

    def _detail_finished(self, event):
        pending = self.detail_pending.pop(event["requestId"], None)
        if not pending or not 0 < pending["bytes"] <= DETAIL_BODY_LIMIT:
            return
        task = asyncio.create_task(self._read_detail_response(event["requestId"], pending["work_id"]))
        self.detail_tasks.add(task)
        task.add_done_callback(self.detail_tasks.discard)

    async def _read_detail_response(self, request_id, work_id):
        try:
            # Read from our bounded inspector cache, after decoded dataLength
            # was confirmed. A small compressed wire length is not sufficient.
            result = await asyncio.wait_for(self.cdp.send("Network.getResponseBody", {"requestId": request_id}), 10)
            text = result["body"]
            if len(text) > 4 * ((DETAIL_BODY_LIMIT + 2) // 3):
                return
            body = base64.b64decode(text, validate=True) if result["base64Encoded"] else text.encode("utf-8")
            if len(body) > DETAIL_BODY_LIMIT:
                return
            work = extract_detail_video(json.loads(body), work_id)
            if work and self.detail_work is None:
                self.detail_work = work
        except LumaBoxError as exc:
            self.detail_error = exc
        except (PlaywrightError, ValueError, RecursionError, asyncio.TimeoutError):
            # A normal response may be incomplete or cancelled by navigation.
            pass

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
                    self.diagnostics = {"stage": failure.get("stage", "page_navigation"), "reason": "Browser network connection failed",
                                        "host": failure["host"]}
                    if failure.get("network_reason"):
                        self.diagnostics["network_reason"] = failure["network_reason"]
                    raise LumaBoxError(failure["code"], "Browser network connection failed", diagnostics=dict(self.diagnostics))

    async def load_gallery_music(self, link):
        """Confirm only the gallery's background player, without requiring images."""
        return await self.load(link, gallery_music=True)

    async def load(self, link, *, gallery_music=False):
        self.expected_identity = (link.work_id, link.kind) if link.work_id else None
        self.diagnostics = {"stage": "input_policy"}
        validate_page_url(link.url)
        host = urlsplit(link.url).hostname
        self.diagnostics = {"stage": "dns_resolution", "host": host}
        try:
            await asyncio.to_thread(resolve_public, host)
        except LumaBoxError as exc:
            self.diagnostics.update(exc.diagnostics, reason=exc.message)
            raise
        self.diagnostics = {"stage": "page_navigation", "host": host}
        try:
            await self.page.goto(link.url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightError:
            self._check_errors(host, navigation_failed=True)
            raise LumaBoxError("RESOURCE_TIMEOUT", "The work page could not load in 30 seconds") from None
        deadline = time.monotonic() + RESOURCE_WAIT
        last = None
        expected_identity = self.expected_identity
        while time.monotonic() < deadline:
            self._check_errors(host)
            self.diagnostics = {"stage": "target_identity"}
            identity = page_identity(self.page.url)
            if identity:
                work_id, kind = identity
                if expected_identity is None:
                    expected_identity = identity
                    self.expected_identity = identity
                if identity != expected_identity:
                    raise LumaBoxError("UNSUPPORTED_MEDIA", "The browser switched to a different work")
                self.diagnostics = {"stage": "media_extraction"}
                try:
                    snapshot = await self.page.evaluate(DOM_SNAPSHOT, {"work_id": work_id, "kind": kind})
                except PlaywrightError as exc:
                    if "Execution context was destroyed" not in exc.message and "Cannot find context with specified id" not in exc.message:
                        raise
                    await asyncio.sleep(0.25)
                    continue
                self._check_errors(host)
                self.diagnostics["resource_hosts"] = sorted({host for r in snapshot["resources"]
                    if (host := _diagnostic_host(r.get("url", "")))})
                self.diagnostics["scope_confirmed"] = bool(snapshot.get("scope_confirmed"))
                self.diagnostics["identity_source"] = snapshot.get("identity_source")
                if snapshot.get("scope_confirmed") and snapshot.get("work_id") == work_id:
                    self.confirmed_work = (work_id, kind, normalize_title(snapshot.get("title")))
                self.diagnostics["detail_response_count"] = self.detail_response_count
                self.diagnostics["detail_metadata_parsed"] = bool(self.detail_work and self.detail_work.work_id == work_id)
                self.diagnostics["media_schemes"] = sorted({
                    scheme if scheme in {"https", "blob"} else "other"
                    for r in snapshot["resources"] if r.get("url")
                    for scheme in [r["url"].partition(":")[0]]})
                # A scoped blob player is media that may still expose a normal
                # HTTPS source; a comment or promotion login is not a view gate.
                has_media = snapshot.get("scope_confirmed") and any(r.get("url") for r in snapshot["resources"])
                if gallery_music:
                    has_media = snapshot.get("scope_confirmed") and any(r.get("url") for r in snapshot.get("gallery_music", []))
                detect_access(snapshot["text"], has_media)
                if gallery_music:
                    try:
                        return extract_gallery_music(snapshot, work_id)
                    except LumaBoxError as exc:
                        if exc.code not in {"NO_AUDIO", "AUDIO_FAILED"}:
                            raise
                        last = exc
                    await asyncio.sleep(0.25)
                    continue
                try:
                    work = extract_work(snapshot, work_id)
                    if not work.title and self.detail_work and self.detail_work.work_id == work_id:
                        work = replace(work, title=self.detail_work.title)
                    return work
                except LumaBoxError as exc:
                    if exc.code != "UNSUPPORTED_MEDIA":
                        self.diagnostics["reason"] = exc.message
                        raise
                    last = exc
                    if kind == "video" and snapshot.get("scope_confirmed") and snapshot.get("count") == 1:
                        if self.detail_error:
                            self.diagnostics["reason"] = self.detail_error.message
                            raise self.detail_error
                        if self.detail_work and self.detail_work.work_id == work_id:
                            self.diagnostics["resource_source"] = "normal_detail_response"
                            self.diagnostics["resource_hosts"] = sorted({_diagnostic_host(r.url) for r in self.detail_work.resources})
                            return replace(self.detail_work, title=normalize_title(snapshot.get("title")) or self.detail_work.title)
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
        # The newly verified CDN works without any session credentials.
        cookies = [] if urlsplit(url).hostname in MEDIA_HOSTS else await self.context.cookies([url])
        headers = {"User-Agent": await self.page.evaluate("navigator.userAgent"), "Referer": self.page.url, "Accept-Encoding": "identity"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return headers
