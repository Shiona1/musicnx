"""Listening history: newest first, with an explicit clear operation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_context, require_mutation_access
from app.core.models import HistoryPage

router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_model=HistoryPage)
async def list_history(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HistoryPage:
    """Read-only; play_history is append-only until the owner clears it."""

    context = get_context(request)
    return await context.history.page(limit=limit, offset=offset)


@router.delete(
    "",
    dependencies=[Depends(require_mutation_access)],
)
async def clear_history(request: Request) -> dict[str, int]:
    context = get_context(request)
    deleted = await context.history.clear()
    return {"deleted": deleted}
