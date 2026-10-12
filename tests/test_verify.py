import hashlib
import pytest
from lumabox.errors import LumaBoxError
from lumabox.verify import verify_file


@pytest.mark.parametrize("index,kind", [(0, "video"), (1, "gallery")])
def test_valid_media_has_dimensions_and_hash(media_fixtures, index, kind):
    file = media_fixtures[index]
    result = verify_file(file, kind)
    assert (result.width, result.height) == (64, 48)
    assert result.sha256 == hashlib.sha256(file.read_bytes()).hexdigest()
    assert result.bytes == file.stat().st_size


@pytest.mark.parametrize("kind", ["video", "gallery"])
def test_html_cannot_pass_as_media(tmp_path, kind):
    file = tmp_path / "fake.mp4"
    file.write_bytes(b"<html>login required</html>")
    with pytest.raises(LumaBoxError, match="VERIFY_FAILED"):
        verify_file(file, kind)


def test_truncated_video_fails_full_verification(tmp_path, media_fixtures):
    file = tmp_path / "broken.mp4"
    file.write_bytes(media_fixtures[0].read_bytes()[:100])
    with pytest.raises(LumaBoxError, match="VERIFY_FAILED"):
        verify_file(file, "video")
