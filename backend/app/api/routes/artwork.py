"""Artwork delivery from the disk-backed cover cache."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.api.deps import get_context
from app.library.covers import ALLOWED_SIZES

router = APIRouter(tags=["artwork"])

_KEY_PATTERN = re.compile(r"^[a-f0-9]{16,64}$")


@router.get("/artwork/{key}")
async def artwork(
    request: Request,
    key: str,
    size: int = Query(default=300),
) -> FileResponse:
    if size not in ALLOWED_SIZES:
        raise HTTPException(status_code=422, detail="unsupported artwork size")
    if not _KEY_PATTERN.fullmatch(key):
        raise HTTPException(status_code=404, detail="artwork not found")
    context = get_context(request)
    path = context.covers.path_for(key, size)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artwork not found")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
