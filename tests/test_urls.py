import pytest

from lumabox.errors import LumaBoxError
from lumabox.urls import parse_input, validate_media_url, validate_page_url, page_identity

VID = "7686430330920075249"


@pytest.mark.parametrize("text,kind,work_id", [
    (f"https://www.douyin.com/video/{VID}", "video", VID),
    ("https://www.douyin.com/note/7695689642767235187?x=1", "gallery", "7695689642767235187"),
    ("3.07 复制 https://v.douyin.com/p6mTe8JN73M/ 打开抖音", None, None),
    ("分享：https://v.douyin.com/p6mTe8JN73M/。", None, None),
])
def test_single_link(text, kind, work_id):
    link = parse_input(text)
    assert (link.kind, link.work_id) == (kind, work_id)


@pytest.mark.parametrize("text", [
    f"http://www.douyin.com/video/{VID}",
    f"https://www.douyin.com.evil.test/video/{VID}",
    f"https://evil.douyin.com/video/{VID}",
    f"https://u@www.douyin.com/video/{VID}",
    f"https://www.douyin.com:444/video/{VID}",
    f"https://www.douyin.com/video/{VID}%0a",
    f"https://www.douyin.com\\evil.test/video/{VID}",
    "https://www.douyin.com/video/123",
    "https://www.douyin.com/user/123456789012345",
    "https://v.douyin.com/a/ https://v.douyin.com/b/",
    "https://v.douyin.com/a/ https://example.com",
    "https://v.douyin.com/a/ HTTPS://v.douyin.com/b/",
    "HtTpS://v.douyin.com/a/ http://example.com",
    "nothing",
])
def test_rejects_ambiguous_or_unsafe_input(text):
    with pytest.raises(LumaBoxError, match="INVALID_INPUT"):
        parse_input(text)


def test_navigation_can_use_share_page_but_input_cannot_use_arbitrary_path():
    assert validate_page_url(f"https://www.iesdouyin.com/share/video/{VID}/")
    with pytest.raises(LumaBoxError):
        parse_input("https://www.douyin.com/settings")


@pytest.mark.parametrize("url", ["https://p3.douyinpic.com/a.jpeg", "https://v1.douyinvod.com/a.mp4"])
def test_observed_media_roots(url):
    assert validate_media_url(url) == url


@pytest.mark.parametrize("url", ["https://douyinpic.com.evil.test/a", "https://notdouyinvod.com/a", "http://p3.douyinpic.com/a", "https://127.0.0.1/a"])
def test_media_rejects_unobserved_targets(url):
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        validate_media_url(url)


def test_observed_modal_page_identity_is_internal_only():
    assert page_identity(f"https://www.douyin.com/jingxuan?modal_id={VID}") == (VID, "video")
    with pytest.raises(LumaBoxError, match="INVALID_INPUT"):
        parse_input(f"https://www.douyin.com/jingxuan?modal_id={VID}")
