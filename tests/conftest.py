import subprocess
import pytest


@pytest.fixture(scope="session")
def media_fixtures(tmp_path_factory):
    directory = tmp_path_factory.mktemp("media")
    video = directory / "valid.mp4"
    image = directory / "valid.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=0.4", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=red:s=64x48", "-frames:v", "1", "-update", "1", str(image)], check=True)
    return video, image
