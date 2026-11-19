"""Sync API — P6, frozen contract in docs/API.md §10.

Status and history are read-only and therefore open on the LAN (ADR-021). The
two mutations — start and cancel — require the shared mutation guard: a valid
PIN session, or loopback when no PIN is configured.

The service is looked up on the application context as ``ctx.sync`` with
``ctx.sync_service`` as a fallback, so this router can be mounted before the
shared context wiring is updated by the integrator (same pattern as lyrics).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_context, require_mutation_access
from app.core.models import SyncHistory, SyncRun, SyncStatus
from app.errors import UnavailableError
from app.sync.service import SyncService

router = APIRouter(prefix="/sync", tags=["sync"])


def _service(request: Request) -> SyncService:
    context = get_context(request)
    service = getattr(context, "sync", None)
    if service is None:
        service = getattr(context, "sync_service", None)
    if service is None:
        raise UnavailableError("sync is not configured")
    return service


@router.get("/status", response_model=SyncStatus)
async def get_sync_status(request: Request) -> SyncStatus:
    """Current run state, the latest run, the scheduler and its progress."""

    return await _service(request).status()


@router.get("/history", response_model=SyncHistory)
async def get_sync_history(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SyncHistory:
    """Run history, newest first (``total`` counts every stored run)."""

    return await _service(request).history(limit)


@router.post(
    "/start",
    status_code=202,
    response_model=SyncStatus,
    dependencies=[Depends(require_mutation_access)],
)
async def start_sync(request: Request) -> SyncStatus:
    """Start a background run; a concurrent run yields ``409 sync_running``."""

    return await _service(request).start(reason="manual")


@router.post(
    "/cancel",
    status_code=202,
    response_model=SyncRun,
    dependencies=[Depends(require_mutation_access)],
)
async def cancel_sync(request: Request) -> SyncRun:
    """Request cancellation between playlists; ``409 sync_not_running`` when idle."""

    return await _service(request).cancel()
