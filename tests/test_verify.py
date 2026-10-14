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


def test_webp_is_decoded_and_named_by_bytes_even_when_url_says_jpeg(tmp_path, webp_fixture):
    image = tmp_path / "01.jpg"
    image.write_bytes(webp_fixture.read_bytes())
    verified = verify_file(image, "gallery")
    assert verified.name == "01.webp"
    assert (verified.width, verified.height) == (64, 48)
    assert verified.sha256 == hashlib.sha256(webp_fixture.read_bytes()).hexdigest()


def test_truncated_webp_does_not_pass_signature_alone(tmp_path, webp_fixture):
    image = tmp_path / "01.jpg"
    image.write_bytes(webp_fixture.read_bytes()[:20])
    with pytest.raises(LumaBoxError, match="VERIFY_FAILED"):
        verify_file(image, "gallery")


def test_video_decode_can_use_more_than_three_minutes(media_fixtures, monkeypatch):
    import subprocess
    import lumabox.verify as verification
    original_run = subprocess.run
    def slow_decoder(command, **kwargs):
        if command[0] == "ffmpeg" and kwargs["timeout"] < 240:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original_run(command, **kwargs)
    monkeypatch.setattr(verification.subprocess, "run", slow_decoder)
    result = verify_file(media_fixtures[0], "video")
    assert (result.width, result.height) == (64, 48)
    assert result.bytes == media_fixtures[0].stat().st_size
