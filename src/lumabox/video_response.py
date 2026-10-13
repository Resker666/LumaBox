"""Read only the target video's normal browser detail response, never call its API."""
import re
from urllib.parse import parse_qs, urlsplit

from .errors import LumaBoxError
from .models import MediaResource, Work
from .urls import https_parts, validate_media_url

DETAIL_BODY_LIMIT = 2 * 1024 * 1024


def detail_request_id(url: str) -> str | None:
    try:
        if len(url) > 16384:
            return None
        parts = https_parts(url)
        if parts.hostname != "www.douyin.com" or parts.path != "/aweme/v1/web/aweme/detail/" or parts.fragment:
            return None
        identifiers = parse_qs(parts.query, max_num_fields=200).get("aweme_id", [])
        if len(identifiers) == 1 and re.fullmatch(r"\d{15,20}", identifiers[0]):
            return identifiers[0]
    except (ValueError, LumaBoxError):
        pass
    return None


def extract_detail_video(data: object, expected_id: str) -> Work | None:
    if not isinstance(data, dict) or type(data.get("status_code")) is not int or data["status_code"] != 0:
        return None
    item = data.get("aweme_detail")
    if not isinstance(item, dict) or item.get("aweme_id") != expected_id or item.get("aweme_type") != 0 or item.get("images"):
        return None
    video = item.get("video")
    if not isinstance(video, dict):
        return None
    # This full H.264 address was verified as a muxed MP4 on 2026-10-13.
    # DASH chunks, audio addresses, recommendations and covers are not inputs.
    address = video.get("play_addr_h264")
    urls = address.get("url_list") if isinstance(address, dict) else None
    if not isinstance(urls, list) or not 1 <= len(urls) <= 10:
        return None
    rejected = None
    for url in urls:
        if not isinstance(url, str) or len(url) > 16384:
            continue
        try:
            validate_media_url(url)
            if urlsplit(url).fragment:
                raise LumaBoxError("UNSUPPORTED_MEDIA", "A complete media address is required")
            return Work(expected_id, "video", (MediaResource(url, 1),))
        except LumaBoxError as exc:
            rejected = exc
    if rejected:
        raise rejected
    return None
