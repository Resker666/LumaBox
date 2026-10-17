import re

from .errors import LumaBoxError
from .models import Work, MediaResource
from .metadata import normalize_title
from .urls import https_parts, validate_media_url

# Based on observed Douyin detail DOM on 2026-10-12/13/16. No application state is read.
DOM_SNAPSHOT = r"""({work_id, kind}) => {
 const visible = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
 const feedSelector = '[data-e2e="feed-active-video"][data-e2e-vid]';
 const detailSelector = kind === 'gallery'
   ? '.note-detail-container[data-e2e="player-container"]'
   : '.video-detail-container[data-e2e="player-container"]';
 const mainSelector = kind === 'gallery' ? '[data-e2e="note-detail"]' : '[data-e2e="video-detail"]';
 const detailIdentity = root => {
   const main = root.closest(mainSelector);
   if (!visible(main) || Array.from(document.querySelectorAll(mainSelector)).filter(visible).length !== 1 ||
       Array.from(main.querySelectorAll(detailSelector)).filter(visible).length !== 1) return null;
   if (kind === 'video') {
     const ids = Array.from(root.classList).filter(c => /^video_\d+$/.test(c)).map(c => c.slice(6));
     return ids.length === 1 ? {id: ids[0], source: 'video_class'} : null;
   }
   const sidebars = Array.from(main.querySelectorAll('.noteSideBarContainer')).filter(visible);
   const active = sidebars.length === 1 ? Array.from(sidebars[0].querySelectorAll(feedSelector)).filter(visible) : [];
   return active.length === 1 ? {id: active[0].getAttribute('data-e2e-vid'), source: 'note_sidebar'} : null;
 };
 const candidates = [];
 for (const root of Array.from(document.querySelectorAll(detailSelector)).filter(visible)) {
   const owner = root.closest(feedSelector);
   const identity = owner ? {id: owner.getAttribute('data-e2e-vid'), source: 'feed'} : detailIdentity(root);
   if (identity?.id === work_id && (kind === 'gallery' || !owner)) candidates.push({root, ...identity});
 }
 if (kind === 'video') {
   for (const root of Array.from(document.querySelectorAll(feedSelector)).filter(visible)) {
     const id = root.getAttribute('data-e2e-vid');
     if (id === work_id) candidates.push({root, id, source: 'feed'});
   }
 }
 const candidate = candidates.length === 1 ? candidates[0] : null;
 const root = candidate?.root;
 const players = root ? Array.from(root.querySelectorAll('.basePlayerContainer')) : [];
 const player = players.length === 1 ? players[0] : null;
 const out = {work_id: candidate?.id || null, identity_source: candidate?.source || null,
   kind, scope_confirmed: !!player, count: 0, resources: [], title: null, text: (document.body?.innerText || '').slice(0, 20000)};
 if (!player) return out;
 // Read caption nodes only inside the region whose identity was established.
 // A dedicated page's caption may be beside the player in the same main.
 try {
   const captionRoot = candidate.source === 'feed' ? root.closest(feedSelector) : root.closest(mainSelector);
   const captions = Array.from(captionRoot?.querySelectorAll('[data-e2e="video-desc"], [data-e2e="note-desc"]') || [])
     .filter(visible).filter(e => !e.closest('.detailrelatedVideoCard') &&
       (!e.closest(feedSelector) || e.closest(feedSelector).getAttribute('data-e2e-vid') === work_id));
   if (captions.length === 1) out.title = (captions[0].innerText || '').slice(0, 3000);
 } catch (_) { /* A missing caption never prevents media extraction. */ }
 if (kind === 'video') {
   const videos = Array.from(player.querySelectorAll('video'));
   if (videos.length === 1) {out.count = 1; out.resources = [{url: videos[0].currentSrc || videos[0].src, order: 1, role: 'video'}];}
 } else {
   const counters = Array.from(root.querySelectorAll('*')).map(e => (e.textContent || '').trim())
     .filter(t => /^\d+\s*\/\s*\d+$/.test(t)).map(t => t.split('/').map(Number).join('/'));
   const pairs = [...new Set(counters)];
   const slides = Array.from(player.querySelectorAll('.dySwiperSlide'));
   // Observed single Live Photo detail pages omit 1/1 but explicitly hide the
   // one-item live progress control. A lone DOM slide alone proves no total.
   const single = pairs.length === 0 && candidate.source === 'note_sidebar' &&
     player.querySelectorAll('.xgplayer-progress.one-live-hide').length === 1 && slides.length === 1 &&
     slides[0].querySelectorAll('video').length === 1;
   if (pairs.length !== 1 && !single) return out;
   const [current, total] = single ? [1, 1] : pairs[0].split('/').map(Number);
   out.count = total;
   const track = slides[0]?.parentElement;
   const trackStyle = track && getComputedStyle(track);
   const viewport = player.getBoundingClientRect();
   // Observed non-looping horizontal layout: the counter independently anchors
   // the viewport's slot. DOM enumeration does not establish image ordinals.
   if (!track || current < 1 || current > total || slides.length !== total ||
       track.children.length !== total || slides.some(s => s.parentElement !== track) ||
       trackStyle.display !== 'flex' || trackStyle.flexDirection !== 'row' || trackStyle.direction !== 'ltr' ||
       viewport.width <= 0 || viewport.height <= 0) return out;
   out.resources = slides.map(slide => {
     const bounds = slide.getBoundingClientRect(), style = getComputedStyle(slide);
     const offset = (bounds.left - viewport.left) / viewport.width;
     const order = current + Math.round(offset);
     const declared = slide.getAttribute('data-swiper-slide-index');
     if (Math.abs(offset - Math.round(offset)) > 0.002 ||
         Math.abs(bounds.width - viewport.width) > 1 || Math.abs(bounds.top - viewport.top) > 1 ||
         Math.abs(bounds.height - viewport.height) > 1 || style.order !== '0' || style.transform !== 'none' ||
         slide.classList.contains('swiper-slide-duplicate') ||
         (declared !== null && (!/^\d+$/.test(declared) || Number(declared) !== order - 1)))
       return {url: '', order: 0, role: 'slide'};
     const foreground = Array.from(slide.querySelectorAll('img.o0KibnKo')).filter(i => i.parentElement?.parentElement === slide);
     // The hidden player outside the slide supplies music; only a video owned
     // by this slide can be its motion companion. Never read arbitrary media responses.
     const videos = Array.from(slide.querySelectorAll('video'));
     const video = videos.length === 1 ? videos[0] : null;
     const motion = video ? video.getAttribute('src') || video.currentSrc || video.querySelector('source')?.src || '' : '';
     return {url: foreground.length === 1 ? foreground[0].currentSrc || foreground[0].src : '', order, role: 'slide',
       motion_required: videos.length > 0, motion_url: motion};
   });
 }
 return out;
}"""


def detect_access(text: str, has_media: bool) -> None:
    text = re.sub(r"\s+", "", text)
    if re.search(r"请完成下方验证|拖动滑块|安全验证|验证你是人类|操作过于频繁", text):
        raise LumaBoxError("CHALLENGE_REQUIRED", "Complete the site's access requirements manually")
    if not has_media and re.search(r"请先登录后(?:观看|访问)|登录后才能(?:观看|访问)", text):
        raise LumaBoxError("AUTH_REQUIRED", "This work requires an account session")


def _is_default_gallery_image(url: str) -> bool:
    # Observed before the real foreground images load on 2026-10-13.
    # This is a pending state, not an additional allowed download host.
    parts = https_parts(url)
    return (parts.hostname == "lf-douyin-pc-web.douyinstatic.com" and
            parts.path == "/obj/douyin-pc-web/ies/douyin_web/media/douyinDefault.ee9da4401be3dae0.png")


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
    has_placeholder = False
    for entry in sorted(entries, key=lambda r: r["order"]):
        url = entry.get("url", "")
        if not url.startswith("https://"):
            raise LumaBoxError("UNSUPPORTED_MEDIA", "A normal HTTPS media resource is not available")
        if kind == "gallery" and _is_default_gallery_image(url):
            has_placeholder = True
            continue
        validate_media_url(url)
        motion = entry.get("motion_url") if kind == "gallery" else None
        if entry.get("motion_required") or motion:
            if not isinstance(motion, str) or not motion.startswith("https://"):
                raise LumaBoxError("UNSUPPORTED_MEDIA", "The slide's motion resource is not available")
            validate_media_url(motion)
        resources.append(MediaResource(url, entry["order"], motion or None))
    if has_placeholder:
        raise LumaBoxError("UNSUPPORTED_MEDIA", "Gallery images are still showing the site's default placeholder")
    return Work(expected_id, kind, tuple(resources), normalize_title(snapshot.get("title")))
