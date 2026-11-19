"""Queue endpoints. Mutations are orchestrated by PlayerManager."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context
from app.core.models import QueueState

router = APIRouter(prefix="/queue", tags=["queue"])


class QueueAddRequest(BaseModel):
    track_ids: list[int] = Field(min_length=1, max_length=500)


class QueueReorderRequest(BaseModel):
    item_ids: list[int]


@router.get("", response_model=QueueState)
async def get_queue(request: Request) -> QueueState:
    return await get_context(request).player.queue_state()


@router.post("", response_model=QueueState, status_code=201)
async def add_to_queue(request: Request, payload: QueueAddRequest) -> QueueState:
    return await get_context(request).player.add_to_queue(payload.track_ids)


@router.delete("/{item_id}", response_model=QueueState)
async def remove_from_queue(request: Request, item_id: int) -> QueueState:
    return await get_context(request).player.remove_queue_item(item_id)


@router.delete("", response_model=QueueState)
async def clear_queue(request: Request) -> QueueState:
    """Remove upcoming items; the current track keeps playing."""

    return await get_context(request).player.clear_queue()


@router.post("/reorder", response_model=QueueState)
async def reorder_queue(request: Request, payload: QueueReorderRequest) -> QueueState:
    return await get_context(request).player.reorder_queue(payload.item_ids)
