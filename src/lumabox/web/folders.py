import os
from pathlib import Path

from .config import WebError


def open_folder(path: Path):
    if os.name != "nt":
        raise WebError("FOLDER_OPEN_UNAVAILABLE", 409)
    try:
        os.startfile(str(path))
    except OSError:
        raise WebError("FOLDER_OPEN_UNAVAILABLE", 409) from None
