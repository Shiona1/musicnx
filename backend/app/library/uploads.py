"""Browser uploads: save a file into the library root, then let P2 import it.

The upload endpoint deliberately does not start an import by itself: several
files can be uploaded first and imported in one job (the importer is
single-flight). Files are streamed to a ``.part`` file, capped by
``library.upload_max_bytes``, renamed atomically and only then exposed to the
importer.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import aiofiles
from fastapi import UploadFile

from app.config import Settings, resolved_import_roots
from app.errors import AppError
from app.logging_setup import get_logger

logger = get_logger("library.uploads")

CHUNK_BYTES = 1024 * 1024
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
ALLOWED_EXTENSIONS = frozenset({".mp3", ".flac", ".aac", ".m4a", ".wav", ".ogg", ".opus"})


@dataclass(frozen=True)
class SavedUpload:
    name: str
    size: int
    path: Path


def uploads_root(settings: Settings) -> Path:
    """Place uploads inside the first import root so P2's ACL accepts them."""

    roots = resolved_import_roots(settings)
    root = roots[0] if roots else settings.paths.library
    return root / "uploads"


def safe_filename(raw: str | None) -> str:
    """Keep the basename, strip control/path characters, enforce an extension."""

    name = Path(raw or "").name.strip()
    if not name:
        raise AppError("a filename is required", code="invalid_filename", status_code=400)
    name = _UNSAFE.sub("_", name)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise AppError(
            "unsupported audio file type",
            code="unsupported_media_type",
            status_code=415,
            details={"extension": suffix or None},
        )
    stem = Path(name).stem.strip(" .") or "track"
    return f"{stem[:120]}{suffix}"


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    counter = 1
    while candidate.exists():
        candidate = directory / f"{Path(filename).stem} ({counter}){Path(filename).suffix}"
        counter += 1
    return candidate


def _tree_size(directory: Path) -> int:
    total = 0
    try:
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _ensure_capacity(
    directory: Path, base_usage: int, free_before: int, new_bytes: int, settings: Settings
) -> None:
    """Reject before the SD card fills; called after the streamed write."""

    quota = int(settings.library.upload_quota_bytes)
    if quota > 0 and base_usage + new_bytes > quota:
        raise AppError(
            "upload quota exceeded",
            code="upload_quota_exceeded",
            status_code=413,
            details={
                "quota_bytes": quota,
                "used_bytes": base_usage,
                "incoming_bytes": new_bytes,
            },
        )
    reserve = int(settings.library.upload_min_free_bytes)
    if free_before - new_bytes < reserve:
        raise AppError(
            "not enough free disk space",
            code="insufficient_storage",
            status_code=507,
            details={"free_bytes": free_before, "reserve_bytes": reserve},
        )


async def save_upload(
    upload: UploadFile,
    settings: Settings,
) -> SavedUpload:
    filename = safe_filename(upload.filename)
    allowed = {item.lower() for item in settings.library.extensions}
    if Path(filename).suffix.lower() not in allowed:
        raise AppError(
            "file type is not enabled in library.extensions",
            code="unsupported_media_type",
            status_code=415,
            details={"extension": Path(filename).suffix.lower()},
        )
    directory = uploads_root(settings)
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    base_usage = await asyncio.to_thread(_tree_size, directory)
    free_before = await asyncio.to_thread(lambda: shutil.disk_usage(directory).free)
    destination = _unique_path(directory, filename)
    partial = destination.with_name(f".{destination.name}.part")
    limit = int(settings.library.upload_max_bytes)
    total = 0
    if upload.size is not None and upload.size > limit:
        raise AppError(
            "file is too large",
            code="file_too_large",
            status_code=413,
            details={"max_bytes": limit},
        )
    try:
        async with aiofiles.open(partial, "wb") as handle:
            while chunk := await upload.read(CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise AppError(
                        "file is too large",
                        code="file_too_large",
                        status_code=413,
                        details={"max_bytes": limit},
                    )
                await handle.write(chunk)
        if total == 0:
            raise AppError("empty file", code="empty_file", status_code=400)
        await asyncio.to_thread(
            _ensure_capacity, directory, base_usage, free_before, total, settings
        )
        await asyncio.to_thread(os.replace, partial, destination)
    except BaseException:
        try:
            await asyncio.to_thread(partial.unlink, True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise
    logger.info("upload saved: name=%s bytes=%s", destination.name, total)
    return SavedUpload(name=destination.name, size=total, path=destination)


__all__ = [
    "ALLOWED_EXTENSIONS",
    "SavedUpload",
    "safe_filename",
    "save_upload",
    "uploads_root",
]
