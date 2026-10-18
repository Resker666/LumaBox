import re
from urllib.parse import unquote, urlsplit, parse_qs

from .errors import LumaBoxError
from .models import InputLink

PAGE_HOSTS = frozenset({"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"})
MEDIA_ROOTS = ("douyinvod.com", "douyinpic.com")
# Exact hosts observed in the target detail response; one full MP4 was decoded.
MEDIA_HOSTS = frozenset({"v5-hl-mly-ov.zjcdn.com", "v5-dy-ov-experiment.zjcdn.com"})
# Confirmed gallery players use numbered nodes in these two music families.
# Match the complete hostname; image/video and redirect policies stay separate.
AUDIO_HOST_PATTERN = re.compile(r"(?:sf[0-9]+-cdn-tos|lf[0-9]+-music-east)\.douyinstatic\.com")
# Observed 302 from the confirmed lf9 music URL; the final MP3 matched the
# original byte-for-byte. This is a redirect destination, not a player source.
AUDIO_REDIRECT_HOSTS = frozenset({"v9-default.tt.x.bsgslb.cn"})
WORK_PATH = re.compile(r"^/(video|note)/(\d{15,20})/?$")
INPUT_URLS = re.compile(r"https?://[^\s<>\"'，。；！）】]+", re.IGNORECASE)


def https_parts(url: str, code: str = "UNSAFE_TARGET"):
    decoded = url
    for _ in range(3):
        decoded = unquote(decoded)
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded) or "\\" in decoded:
        raise LumaBoxError(code, "Malformed URL")
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None or parts.port not in (None, 443):
            raise ValueError
        if parts.hostname.endswith(".") or "%" in parts.netloc or any(c.isspace() for c in url):
            raise ValueError
    except ValueError:
        raise LumaBoxError(code, "HTTPS URL with an allowed host is required") from None
    return parts


def validate_page_url(url: str) -> str:
    if https_parts(url).hostname not in PAGE_HOSTS:
        raise LumaBoxError("UNSAFE_TARGET", "Page host is not allowed")
    return url


def validate_media_url(url: str) -> str:
    host = https_parts(url).hostname
    if host not in MEDIA_HOSTS and not any(host == root or host.endswith("." + root) for root in MEDIA_ROOTS):
        raise LumaBoxError("UNSAFE_TARGET", "Media host has not been verified")
    return url


def validate_audio_url(url: str, *, redirect: bool = False) -> str:
    try:
        parts = https_parts(url)
    except LumaBoxError as exc:
        exc.diagnostics.update(policy_reason="AUDIO_URL_INVALID")
        raise
    if parts.fragment:
        raise LumaBoxError("UNSAFE_TARGET", "Audio URL has an unsupported fragment",
                          diagnostics={"policy_reason": "AUDIO_URL_INVALID", "host": parts.hostname})
    if not AUDIO_HOST_PATTERN.fullmatch(parts.hostname) and not (redirect is True and parts.hostname in AUDIO_REDIRECT_HOSTS):
        raise LumaBoxError("UNSAFE_TARGET", "Audio host has not been verified",
                          diagnostics={"policy_reason": "AUDIO_HOST_UNSUPPORTED", "host": parts.hostname})
    return url


def page_identity(url: str) -> tuple[str, str] | None:
    validate_page_url(url)
    match = WORK_PATH.fullmatch(urlsplit(url).path)
    if not match:
        parts = urlsplit(url)
        modal = parse_qs(parts.query).get("modal_id", [])
        if parts.path == "/jingxuan" and len(modal) == 1 and re.fullmatch(r"\d{15,20}", modal[0]):
            return modal[0], "video"
        return None
    return match[2], "video" if match[1] == "video" else "gallery"


def parse_input(text: str) -> InputLink:
    matches = extract_input_urls(text)
    if len(matches) != 1:
        raise LumaBoxError("INVALID_INPUT", "Provide exactly one supported link")
    url = matches[0].rstrip(".,;!)]}")
    parts = https_parts(url, "INVALID_INPUT")
    if parts.hostname not in PAGE_HOSTS:
        raise LumaBoxError("INVALID_INPUT", "Unsupported page host")
    identity = page_identity(url)
    if identity and WORK_PATH.fullmatch(parts.path):
        return InputLink(url, *identity)
    if parts.hostname == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+/?", parts.path):
        return InputLink(url, None, None)
    raise LumaBoxError("INVALID_INPUT", "Provide a video, note or short link")


def extract_input_urls(text: str) -> list[str]:
    return [match.rstrip(".,;!)]}") for match in INPUT_URLS.findall(text)]


def canonical_page_url(link: InputLink) -> str:
    if link.work_id:
        return f"https://www.douyin.com/{'video' if link.kind == 'video' else 'note'}/{link.work_id}"
    return f"https://v.douyin.com{urlsplit(link.url).path}"
