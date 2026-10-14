import argparse
import asyncio
import json
from pathlib import Path

from .runner import run_download
from .limits import DEFAULT_VIDEO_LIMIT_MIB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumabox", description="LumaBox: download and verify one authorized Douyin work")
    parser.add_argument("input", help="One video/note link, short link or share text")
    parser.add_argument("--authorized", action="store_true", required=True, help="Confirm that you have permission to download this work")
    parser.add_argument("--output", type=Path, default=Path("Downloaded"))
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window")
    parser.add_argument("--video-limit-mib", type=int, default=DEFAULT_VIDEO_LIMIT_MIB, help="Video size limit in MiB (1–8192, default 1024)")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(run_download(args.input, args.output, args.headless, video_limit_mib=args.video_limit_mib))
    except KeyboardInterrupt:
        result = {"status": "failed", "error": "INTERRUPTED"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 130 if result.get("error") == "INTERRUPTED" else 1
