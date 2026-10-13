import copy
import pytest
from lumabox.errors import LumaBoxError
from lumabox.extract import extract_work

ID = "7695689642767235187"


@pytest.fixture
def gallery():
    return {"work_id": ID, "kind": "gallery", "scope_confirmed": True, "count": 3,
            "resources": [{"order": 2, "url": "https://p3.douyinpic.com/b.jpeg", "role": "slide"},
                          {"order": 1, "url": "https://p3.douyinpic.com/a.jpeg", "role": "slide"},
                          {"order": 3, "url": "https://p3.douyinpic.com/a.jpeg", "role": "slide"},
                          {"order": 1, "url": "https://p3.douyinpic.com/a.jpeg", "role": "background"},
                          {"order": 0, "url": "https://example.com/avatar", "role": "recommendation"}]}


def test_gallery_retains_order_and_real_duplicate(gallery):
    work = extract_work(gallery, ID)
    assert [r.order for r in work.resources] == [1, 2, 3]
    assert work.resources[0].url == work.resources[2].url


@pytest.mark.parametrize("changes,code", [({"work_id": "7686430330920075249"}, "UNSUPPORTED_MEDIA"),
    ({"scope_confirmed": False}, "UNSUPPORTED_MEDIA"), ({"count": 12}, "UNSUPPORTED_MEDIA"),
    ({"count": 51}, "SIZE_LIMIT"), ({"count": 0}, "UNSUPPORTED_MEDIA")])
def test_incomplete_or_unmatched_work_fails(gallery, changes, code):
    gallery.update(changes)
    with pytest.raises(LumaBoxError, match=code):
        extract_work(gallery, ID)


def test_blob_is_not_a_downloadable_resource():
    with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
        extract_work({"work_id": ID, "kind": "video", "scope_confirmed": True, "count": 1,
                      "resources": [{"url": "blob:example", "order": 1, "role": "video"}]}, ID)


def test_observed_gallery_default_image_is_pending_media(gallery):
    gallery["resources"][0]["url"] = "https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/ies/douyin_web/media/douyinDefault.ee9da4401be3dae0.png"
    with pytest.raises(LumaBoxError, match="UNSUPPORTED_MEDIA"):
        extract_work(gallery, ID)


@pytest.mark.parametrize("url", [
    "https://lf-douyin-pc-web.douyinstatic.com/other.png",
    "https://lf-douyin-pc-web.douyinstatic.com.evil.test/obj/douyin-pc-web/ies/douyin_web/media/douyinDefault.ee9da4401be3dae0.png",
])
def test_other_static_hosts_or_assets_are_not_media(gallery, url):
    gallery["resources"][0]["url"] = url
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        extract_work(gallery, ID)


def test_default_image_does_not_hide_a_different_unverified_host(gallery):
    gallery["resources"][0]["url"] = "https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/ies/douyin_web/media/douyinDefault.ee9da4401be3dae0.png"
    gallery["resources"][1]["url"] = "https://unverified.example/image.jpeg"
    with pytest.raises(LumaBoxError, match="UNSAFE_TARGET"):
        extract_work(gallery, ID)
