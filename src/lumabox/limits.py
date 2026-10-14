from .errors import LumaBoxError

MIB = 1024 * 1024
DEFAULT_VIDEO_LIMIT_MIB = 1024
MAX_VIDEO_LIMIT_MIB = 8192
IMAGE_LIMIT = 20 * MIB
GALLERY_LIMIT = 200 * MIB


def video_limit_bytes(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_VIDEO_LIMIT_MIB:
        raise LumaBoxError("INVALID_INPUT", "Video size limit must be an integer from 1 to 8192 MiB")
    return value * MIB
