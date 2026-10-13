import asyncio
import json
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .browser import BrowserSession
from .errors import LumaBoxError
from .transfer import download_resource
from .urls import parse_input
from .verify import verify_file

VIDEO_LIMIT = 100 * 1024 * 1024
IMAGE_LIMIT = 20 * 1024 * 1024
GALLERY_LIMIT = 200 * 1024 * 1024


def make_zip(directory: Path, work_id: str, names: list[str]):
    with zipfile.ZipFile(directory / f"{work_id}-images.zip", "x", compression=zipfile.ZIP_STORED) as archive:
        for name in [*names, "report.json"]:
            archive.write(directory / name, arcname=name)


async def _blocking(fn, *args, cancel):
    worker = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        # Finish the worker before closing its browser or removing its files.
        try:
            await asyncio.shield(worker)
        except Exception:
            pass
        raise


def _write_report(path: Path, result: dict):
    with path.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")


async def run_download(text: str, output: Path, headless: bool = False) -> dict:
    run_id = uuid.uuid4().hex
    root = output.resolve()
    temporary = None
    session = None
    stage = "input"
    failure_diagnostics = {}
    cancel = threading.Event()
    result = {"run_id": run_id, "date": datetime.now(timezone.utc).isoformat(), "status": "failed",
              "work_id": None, "kind": None, "metadata_parsed": False, "expected_count": 0,
              "actual_count": 0, "files": [], "bytes": 0, "checks": {"full_decode": False},
              "headless": headless, "http_status": None, "output_directory": None}
    try:
        link = parse_input(text)
        result.update(work_id=link.work_id, kind=link.kind)
        stage = "output_setup"
        root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".partial-{run_id}-", dir=root))
        stage = "browser_start"
        async with BrowserSession(headless=headless) as session:
            stage = "browser_load"
            work = await session.load(link)
            result.update(work_id=work.work_id, kind=work.kind, metadata_parsed=True, expected_count=len(work.resources))
            loop = asyncio.get_running_loop()

            def headers(url):
                if cancel.is_set():
                    raise LumaBoxError("INTERRUPTED", "Download cancelled")
                future = asyncio.run_coroutine_threadsafe(session.media_headers(url), loop)
                try:
                    return future.result(timeout=15)
                except BaseException:
                    future.cancel()
                    raise

            for resource in work.resources:
                name = f"{work.work_id}.mp4" if work.kind == "video" else f"{resource.order:02d}.jpg"
                path = temporary / name
                limit = VIDEO_LIMIT if work.kind == "video" else min(IMAGE_LIMIT, GALLERY_LIMIT - result["bytes"])
                if limit <= 0:
                    raise LumaBoxError("SIZE_LIMIT", "Gallery exceeds the total size limit")
                stage = "media_transfer"
                count = await _blocking(download_resource, resource, path, headers, limit, cancel, cancel=cancel)
                if count > limit:
                    raise LumaBoxError("SIZE_LIMIT", "Media exceeds the size limit")
                stage = "media_verification"
                verified = await _blocking(verify_file, path, work.kind, cancel=cancel)
                if verified.name != path.name:
                    path.rename(temporary / verified.name)
                result["files"].append(asdict(verified))
                result["bytes"] += verified.bytes
        result["http_status"] = session.http_status
        stage = "publication"
        destination = root / f"{work.work_id}-{run_id}"
        result.update(status="success", actual_count=len(result["files"]), output_directory=str(destination),
                      checks={"full_decode": True}, report_path=str(destination / "report.json"))
        _write_report(temporary / "report.json", result)
        if work.kind == "gallery":
            make_zip(temporary, work.work_id, [file["name"] for file in result["files"]])
        temporary.rename(destination)
        temporary = None
        return result
    except asyncio.CancelledError:
        result["error"] = "INTERRUPTED"
    except LumaBoxError as exc:
        result["error"] = exc.code
        failure_diagnostics = {"reason": exc.message, **exc.diagnostics}
    except Exception:
        result["error"] = "DOWNLOAD_FAILED"
    finally:
        if temporary is not None:
            try:
                if temporary.parent.resolve() != root or not temporary.name.startswith(f".partial-{run_id}-"):
                    raise ValueError("Unexpected temporary directory")
                if temporary.is_symlink():
                    temporary.unlink()
                else:
                    shutil.rmtree(temporary)
            except (OSError, ValueError):
                result["cleanup_failed"] = True
    if session is not None:
        result["http_status"] = getattr(session, "http_status", None)
    result["diagnostics"] = {"stage": stage, **failure_diagnostics}
    if stage == "browser_load" and session is not None:
        result["diagnostics"].update(getattr(session, "diagnostics", {}))
        blocked = getattr(session, "blocked_documents", [])
        if blocked:
            result["diagnostics"]["blocked_documents"] = blocked
        result["diagnostics"].update(
            media_responses=getattr(session, "media_responses", []),
            media_response_count=getattr(session, "media_response_count", 0),
            media_response_truncated=getattr(session, "media_response_truncated", False))
    result.update(status="failed", actual_count=0, files=[], bytes=0, checks={"full_decode": False}, output_directory=None)
    try:
        root.mkdir(parents=True, exist_ok=True)
        report = root / f"{run_id}-failed.json"
        result["report_path"] = str(report)
        _write_report(report, result)
    except OSError:
        result["report_write_failed"] = True
        result["report_path"] = None
    return result
