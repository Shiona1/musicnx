"""Downloads API — P4, frozen contract in docs/API.md §8.

Read-only listing stays open (LAN MVP); every mutation is guarded by
``require_mutation_access``. The manager is looked up on the application
context as ``ctx.downloads`` with ``ctx.download_manager`` as a fallback, so the
router can be mounted before/without touching the shared context wiring.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.core.models import (
    DownloadRequest,
    DownloadStatus,
    DownloadTask,
    DownloadTaskPage,
)
from app.downloads.base import DownloadManager
from app.errors import AppError, NotFoundError, UnavailableError

router = APIRouter(prefix="/downloads", tags=["downloads"])


class DownloadTasksResponse(BaseModel):
    """``202`` body of ``POST /downloads`` (docs/API.md §8.2)."""

    tasks: list[DownloadTask] = Field(default_factory=list)


def _manager(request: Request) -> DownloadManager:
    context = get_context(request)
    manager = getattr(context, "downloads", None)
    if manager is None:
        manager = getattr(context, "download_manager", None)
    if manager is None:
        raise UnavailableError("downloads are not configured")
    return manager


def _selector(payload: DownloadRequest) -> str:
    """Exactly one of track_id/album_id/playlist_id must be present."""

    selected = [
        name
        for name in ("track_id", "album_id", "playlist_id")
        if getattr(payload, name) is not None
    ]
    if len(selected) != 1:
        raise AppError(
            "exactly one of track_id, album_id or playlist_id is required",
            details={"fields": selected},
        )
    return selected[0]


def _positive(value: int, field: str) -> int:
    if value <= 0:
        raise AppError(f"{field} must be a positive integer", details={field: value})
    return value


@router.get("", response_model=DownloadTaskPage)
async def list_downloads(
    request: Request,
    status: Annotated[DownloadStatus | None, Query()] = None,
) -> DownloadTaskPage:
    """Queue plus history, newest first; optional ``status`` filter."""

    return await _manager(request).list(status=status)


@router.post(
    "",
    status_code=202,
    response_model=DownloadTasksResponse,
    dependencies=[Depends(require_mutation_access)],
)
async def create_downloads(
    request: Request, payload: DownloadRequest
) -> DownloadTasksResponse:
    """Queue a track, an album or a playlist and return the affected tasks."""

    manager = _manager(request)
    field = _selector(payload)
    priority = payload.priority
    if field == "track_id":
        task = await manager.start_track(
            _positive(payload.track_id or 0, "track_id"), priority=priority
        )
        tasks = [task]
    elif field == "album_id":
        tasks = await manager.start_album(
            _positive(payload.album_id or 0, "album_id"), priority=priority
        )
    else:
        playlist_id = (payload.playlist_id or "").strip()
        if not playlist_id:
            raise AppError("playlist_id must not be empty")
        tasks = await manager.start_playlist(playlist_id, priority=priority)
    return DownloadTasksResponse(tasks=tasks)


@router.delete(
    "/{task_id}",
    status_code=204,
    response_model=None,
    dependencies=[Depends(require_mutation_access)],
)
async def delete_download(request: Request, task_id: int) -> None:
    """Cancel an active task and remove its queue record (temp file cleanup)."""

    await _manager(request).cancel(task_id, remove=True)


@router.post(
    "/{task_id}/cancel",
    response_model=DownloadTask,
    dependencies=[Depends(require_mutation_access)],
)
async def cancel_download(request: Request, task_id: int) -> DownloadTask:
    """Soft cancel: ``status=cancelled``, the partial file is kept for resume."""

    task = await _manager(request).cancel(task_id)
    if task is None:  # pragma: no cover - manager raises NotFoundError first
        raise NotFoundError("Download task not found", details={"id": task_id})
    return task


@router.post(
    "/{task_id}/retry",
    response_model=DownloadTask,
    dependencies=[Depends(require_mutation_access)],
)
async def retry_download(request: Request, task_id: int) -> DownloadTask:
    """Reset attempts/error and queue the task again."""

    return await _manager(request).retry(task_id)
