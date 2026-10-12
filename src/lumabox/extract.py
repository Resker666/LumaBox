import re

from .errors import LumaBoxError
from .models import Work, MediaResource
from .urls import validate_media_url

# Based on observed Douyin detail DOM on 2026-10-12/13. No application state is read.
DOM_SNAPSHOT = r"""({work_id, kind}) => {
 const visible = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
 const candidates = kind === 'gallery'
   ? Array.from(document.querySelectorAll('.note-detail-container[data-e2e="player-container"]'))
   : Array.from(document.querySelectorAll('[data-e2e="feed-active-video"][data-e2e-vid]'))
       .filter(e => e.getAttribute('data-e2e-vid') === work_id);
 const roots = candidates.filter(visible);
 const root = roots.length === 1 ? roots[0] : null;
 const players = root ? Array.from(root.querySelectorAll('.basePlayerContainer')) : [];
 const player = players.length === 1 ? players[0] : null;
 const out = {work_id, kind, scope_confirmed: !!player, count: 0, resources: [], text: (document.body?.innerText || '').slice(0, 20000)};
 if (!player) return out;
 if (kind === 'video') {
   const videos = Array.from(player.querySelectorAll('video'));
   if (videos.length === 1) {out.count = 1; out.resources = [{url: videos[0].currentSrc || videos[0].src, order: 1, role: 'video'}];}
 } else {
   const counters = Array.from(root.querySelectorAll('*')).map(e => (e.textContent || '').trim())
     .filter(t => /^\d+\s*\/\s*\d+$/.test(t)).map(t => Number(t.split('/')[1]));
   const totals = [...new Set(counters)];
   out.count = totals.length === 1 ? totals[0] : 0;
   const slides = Array.from(player.querySelectorAll('.dySwiperSlide'));
   out.resources = slides.map((slide, index) => {
     const foreground = Array.from(slide.querySelectorAll('img.o0KibnKo')).filter(i => i.parentElement?.parentElement === slide);
     return {url: foreground.length === 1 ? foreground[0].currentSrc || foreground[0].src : '', order: index + 1, role: 'slide'};
   });
 }
 return out;
}"""


def detect_access(text: str, has_media: bool) -> None:
    if re.search(r"请完成下方验证|拖动滑块|安全验证|验证你是人类|操作过于频繁", text):
        raise LumaBoxError("CHALLENGE_REQUIRED", "Complete the site's access requirements manually")
    if not has_media and re.search(r"请先登录后|登录后才能(?:观看|访问)", text):
        raise LumaBoxError("AUTH_REQUIRED", "This work requires an account session")


def extract_work(snapshot: dict, expected_id: str) -> Work:
    if snapshot.get("work_id") != expected_id or not snapshot.get("scope_confirmed"):
        raise LumaBoxError("UNSUPPORTED_MEDIA", "Target work region could not be confirmed")
    kind = snapshot.get("kind")
    count = snapshot.get("count", 0)
    if kind == "gallery" and isinstance(count, int) and count > 50:
        raise LumaBoxError("SIZE_LIMIT", "Gallery exceeds 50 images")
    if kind not in ("video", "gallery") or not isinstance(count, int) or not 1 <= count <= 50 or (kind == "video" and count != 1):
        raise LumaBoxError("UNSUPPORTED_MEDIA", "Complete media count could not be confirmed")
    role = "video" if kind == "video" else "slide"
    entries = [r for r in snapshot.get("resources", []) if r.get("role") == role]
    if len(entries) != count or sorted(r.get("order", 0) for r in entries) != list(range(1, count + 1)):
        raise LumaBoxError("UNSUPPORTED_MEDIA", "The complete ordered work was not available")
    resources = []
    for entry in sorted(entries, key=lambda r: r["order"]):
        url = entry.get("url", "")
        if not url.startswith("https://"):
            raise LumaBoxError("UNSUPPORTED_MEDIA", "A normal HTTPS media resource is not available")
        validate_media_url(url)
        resources.append(MediaResource(url, entry["order"]))
    return Work(expected_id, kind, tuple(resources))
