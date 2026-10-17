import argparse
import asyncio
import json
from pathlib import Path

from .runner import run_download
from .limits import DEFAULT_VIDEO_LIMIT_MIB
from .progress import validate_progress_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumabox", description="LumaBox: download and verify one authorized Douyin work")
    parser.add_argument("input", help="One video/note link, short link or share text")
    parser.add_argument("--authorized", action="store_true", required=True, help="Confirm that you have permission to download this work")
    parser.add_argument("--output", type=Path, default=Path("Downloaded"))
    parser.add_argument("--headless", action="store_true", help="Run Chromium without a visible window")
    parser.add_argument("--progress-file", type=Path, help="Private progress snapshot at <output>/.progress.json")
    parser.add_argument("--video-limit-mib", type=int, default=DEFAULT_VIDEO_LIMIT_MIB, help="Video size limit in MiB (1–8192, default 1024)")
    args = parser.parse_args(argv)
    if args.progress_file is not None:
        try:
            args.progress_file = validate_progress_path(args.output, args.progress_file)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    options = {"video_limit_mib": args.video_limit_mib}
    if args.progress_file is not None:
        options["progress_file"] = args.progress_file
    try:
        result = asyncio.run(run_download(args.input, args.output, args.headless, **options))
    except KeyboardInterrupt:
        result = {"status": "failed", "error": "INTERRUPTED"}
    # ASCII JSON is also valid UTF-8, independent of the Windows console locale.
    # Escapes preserve Chinese titles, emoji and paths when the worker decodes it.
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "success" else 130 if result.get("error") == "INTERRUPTED" else 1
