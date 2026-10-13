import hashlib
import json
import subprocess
from pathlib import Path

from .errors import LumaBoxError
from .models import VerifiedFile


def verify_file(path: Path, kind: str) -> VerifiedFile:
    try:
        with path.open("rb") as file:
            signature = file.read(32)
        input_options = []
        if kind == "video":
            if len(signature) < 12 or signature[4:8] != b"ftyp":
                raise ValueError
        elif kind == "gallery":
            if signature.startswith(b"\xff\xd8\xff"):
                codec, extension = "mjpeg", ".jpg"
                input_options = ["-f", "jpeg_pipe"]
            elif len(signature) >= 12 and signature[:4] == b"RIFF" and signature[8:12] == b"WEBP":
                codec, extension = "webp", ".webp"
                input_options = ["-f", "webp_pipe"]
            else:
                raise ValueError
        else:
            raise ValueError
        probe = subprocess.run(["ffprobe", "-v", "error", *input_options, "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, timeout=180, check=True)
        data = json.loads(probe.stdout)
        streams = data.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        if not videos:
            raise ValueError
        stream = videos[0]
        if kind == "video" and "mp4" not in data["format"].get("format_name", "").split(","):
            raise ValueError
        if kind == "gallery" and stream.get("codec_name") != codec:
            raise ValueError
        width, height = int(stream["width"]), int(stream["height"])
        if width <= 0 or height <= 0:
            raise ValueError
        duration = float(data["format"]["duration"]) if kind == "video" else None
        if kind == "video" and duration <= 0:
            raise ValueError
        command = ["ffmpeg", "-v", "error", "-xerror", "-nostdin", *input_options, "-i", str(path), "-map", "0:v:0"]
        if kind == "video":
            command.extend(["-map", "0:a?"])
        subprocess.run(command + ["-f", "null", "-"], capture_output=True, timeout=180, check=True)
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        name = path.with_suffix(extension).name if kind == "gallery" else path.name
        return VerifiedFile(name, path.stat().st_size, digest, width, height, duration)
    except FileNotFoundError:
        raise LumaBoxError("DEPENDENCY_MISSING", "Check the file and FFmpeg/FFprobe installation") from None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise LumaBoxError("VERIFY_FAILED", "The complete media file failed verification") from None
