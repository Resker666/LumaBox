import asyncio
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
        try:
            self.guard.check(event["request"]["url"], event.get("redirectedRequestId"), event["requestId"])
            await self.cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except LumaBoxError as exc:
            self.error = exc
            await self.cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
        except PlaywrightError:
            # Context shutdown can cancel paused requests.
            pass

    def _response(self, response):
        if response.request.is_navigation_request() and response.request.frame == self.page.main_frame:
            self.http_status = response.status
            try:
                check_status(response.status)
            except LumaBoxError as exc:
                self.error = exc

    def _check_errors(self, initial_host):
        if self.error:
            raise self.error
        for failure in self.proxy.failures:
            if failure["host"] == initial_host:
                raise LumaBoxError(failure["code"], "Browser network connection failed")

    async def load(self, link):
        validate_page_url(link.url)
        host = urlsplit(link.url).hostname
        await asyncio.to_thread(resolve_public, host)
        try:
            await self.page.goto(link.url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightError:
            self._check_errors(host)
            raise LumaBoxError("RESOURCE_TIMEOUT", "The work page could not load in 30 seconds") from None
        deadline = time.monotonic() + RESOURCE_WAIT
        last = None
        expected_identity = (link.work_id, link.kind) if link.work_id else None
        while time.monotonic() < deadline:
            self._check_errors(host)
            identity = page_identity(self.page.url)
            if identity:
                work_id, kind = identity
                if expected_identity is None:
                    expected_identity = identity
                if identity != expected_identity:
                    raise LumaBoxError("UNSUPPORTED_MEDIA", "The browser switched to a different work")
                snapshot = await self.page.evaluate(DOM_SNAPSHOT, {"work_id": work_id, "kind": kind})
                detect_access(snapshot["text"], any(r.get("url", "").startswith("https://") for r in snapshot["resources"]))
                try:
                    return extract_work(snapshot, work_id)
                except LumaBoxError as exc:
                    if exc.code != "UNSUPPORTED_MEDIA":
                        raise
                    last = exc
            else:
                text = await self.page.locator("body").inner_text(timeout=1000)
                detect_access(text[:20000], False)
            await asyncio.sleep(0.25)
        if last:
            raise last
        raise LumaBoxError("RESOURCE_TIMEOUT", "Target resources were not available in 45 seconds")

    async def media_headers(self, url: str) -> dict[str, str]:
        validate_media_url(url)
        cookies = await self.context.cookies([url])
        headers = {"User-Agent": await self.page.evaluate("navigator.userAgent"), "Referer": self.page.url, "Accept-Encoding": "identity"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return headers
