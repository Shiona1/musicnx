"""Lyrics API — P5, frozen contract in docs/API.md §9.

Reading lyrics is open (LAN MVP, like the rest of the read-only surface); the
offset mutation is guarded by ``require_mutation_access`` (PIN or localhost).
The service is looked up on the application context as ``ctx.lyrics`` with
``ctx.lyrics_service`` as a fallback, so this router can be mounted before the
shared context wiring is updated by the integrator.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.core.models import LyricsDTO
from app.errors import UnavailableError
from app.lyrics.service import OFFSET_MAX_MS, OFFSET_MIN_MS, LyricsService

router = APIRouter(prefix="/lyrics", tags=["lyrics"])

#: BCP-47-ish language tag: ``ru``, ``en-US``, ``und``. Kept permissive enough
#: for real content but bounded so the value can never be used as a free-form
#: cache key or log payload.
_LANGUAGE_PATTERN = r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"


class LyricsOffsetRequest(BaseModel):
    """Body of ``PUT /lyrics/{track_id}/offset`` (docs/API.md §9)."""

    offset_ms: int = Field(ge=OFFSET_MIN_MS, le=OFFSET_MAX_MS)


def _service(request: Request) -> LyricsService:
    context = get_context(request)
    service = getattr(context, "lyrics", None)
    if service is None:
        service = getattr(context, "lyrics_service", None)
    if service is None:
        raise UnavailableError("lyrics are not configured")
    return service


@router.get("/{track_id}", response_model=LyricsDTO)
async def get_lyrics(
    request: Request,
    track_id: int,
    language: Annotated[
        str | None, Query(pattern=_LANGUAGE_PATTERN, max_length=32)
    ] = None,
) -> LyricsDTO:
    """Lyrics for one track: cache, local ``.lrc`` or Yandex, in that order.

    ``404`` when the track does not exist; a track without lyrics returns
    ``200`` with ``available=false``.
    """

    return await _service(request).get(track_id, language=language)


@router.put(
    "/{track_id}/offset",
    response_model=LyricsDTO,
    dependencies=[Depends(require_mutation_access)],
)
async def set_lyrics_offset(
    request: Request, track_id: int, payload: LyricsOffsetRequest
) -> LyricsDTO:
    """Persist the per-track karaoke offset and return the updated lyrics."""

    return await _service(request).set_offset(track_id, payload.offset_ms)
