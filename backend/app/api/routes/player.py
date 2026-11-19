"""Player control endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context
from app.core.models import PlayerState

router = APIRouter(prefix="/player", tags=["player"])


class PlayRequest(BaseModel):
    track_id: int | None = None


class SeekRequest(BaseModel):
    position_ms: int = Field(ge=0, le=86_400_000)


class VolumeRequest(BaseModel):
    volume: int = Field(ge=0, le=100)


class PlayerModeRequest(BaseModel):
    """At least one field is required; unset fields keep their current mode."""

    shuffle: bool | None = None
    repeat: Literal["off", "one", "all"] | None = None


@router.get("/state", response_model=PlayerState)
async def get_state(request: Request) -> PlayerState:
    return await get_context(request).player.snapshot()


@router.post("/play", response_model=PlayerState)
async def play(request: Request, payload: PlayRequest | None = None) -> PlayerState:
    track_id = payload.track_id if payload is not None else None
    return await get_context(request).player.play(track_id)


@router.post("/pause", response_model=PlayerState)
async def pause(request: Request) -> PlayerState:
    return await get_context(request).player.pause()


@router.post("/stop", response_model=PlayerState)
async def stop(request: Request) -> PlayerState:
    return await get_context(request).player.stop()


@router.post("/next", response_model=PlayerState)
async def next_track(request: Request) -> PlayerState:
    return await get_context(request).player.next_track()


@router.post("/previous", response_model=PlayerState)
async def previous_track(request: Request) -> PlayerState:
    return await get_context(request).player.previous_track()


@router.post("/seek", response_model=PlayerState)
async def seek(request: Request, payload: SeekRequest) -> PlayerState:
    return await get_context(request).player.seek(payload.position_ms)


@router.post("/volume", response_model=PlayerState)
async def volume(request: Request, payload: VolumeRequest) -> PlayerState:
    return await get_context(request).player.set_volume(payload.volume)


@router.post("/mode", response_model=PlayerState)
async def set_mode(request: Request, payload: PlayerModeRequest) -> PlayerState:
    return await get_context(request).player.set_mode(
        shuffle=payload.shuffle, repeat=payload.repeat
    )
