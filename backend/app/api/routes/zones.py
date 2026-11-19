"""Zones API — P7, frozen contract in docs/API.md §11.

Reading the zone list is open on the LAN (ADR-021); activation, volume and
per-zone offset are mutations and require the shared mutation guard (a valid PIN
session, or loopback when no PIN is configured).

The manager is looked up as ``ctx.zones`` with ``ctx.zone_manager`` as a fallback
and, when neither exists yet, built from ``ctx.settings/db/bus/player`` and cached
on the context. That keeps this router mountable before shared context wiring is
updated by the integrator (same pattern as lyrics/sync).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.core.models import AudioZone, ZonePage
from app.errors import UnavailableError
from app.zones.manager import ZoneManager, create_zone_manager
from app.zones.repository import ZONE_OFFSET_MAX_MS, ZONE_OFFSET_MIN_MS

router = APIRouter(prefix="/zones", tags=["zones"])


class ZoneVolumeRequest(BaseModel):
    """Body of ``POST /zones/{id}/volume`` (docs/API.md §11)."""

    volume: int = Field(ge=0, le=100)


class ZoneOffsetRequest(BaseModel):
    """Body of ``POST /zones/{id}/offset`` (karaoke delay, milliseconds)."""

    offset_ms: int = Field(ge=ZONE_OFFSET_MIN_MS, le=ZONE_OFFSET_MAX_MS)


def resolve_zone_manager(context: Any) -> ZoneManager:
    """Return the wired zone manager, building it lazily when possible."""

    manager = getattr(context, "zones", None)
    if manager is None:
        manager = getattr(context, "zone_manager", None)
    if manager is not None:
        return manager

    settings = getattr(context, "settings", None)
    db = getattr(context, "db", None)
    bus = getattr(context, "bus", None)
    if settings is None or db is None or bus is None:
        raise UnavailableError("zones are not configured")

    manager = create_zone_manager(
        settings, bus, db, getattr(context, "player", None)
    )
    try:
        context.zones = manager
    except (AttributeError, TypeError):  # pragma: no cover - frozen contexts
        pass
    return manager


def _manager(request: Request) -> ZoneManager:
    return resolve_zone_manager(get_context(request))


@router.get("", response_model=ZonePage)
async def list_zones(request: Request) -> ZonePage:
    """All zones with the active one flagged; defaults are seeded on first use."""

    # Reconcile on read so a speaker connected outside the app (bluetoothctl,
    # desktop, auto-reconnect) shows up without a restart. No event is published
    # here: polling must not turn into a zones.updated storm.
    return await _manager(request).refresh(publish=False)


@router.post(
    "/{zone_id}/activate",
    response_model=AudioZone,
    dependencies=[Depends(require_mutation_access)],
)
async def activate_zone(request: Request, zone_id: str) -> AudioZone:
    """Move playback to the zone's sink; the current track is never stopped."""

    return await _manager(request).activate(zone_id)


@router.post(
    "/{zone_id}/volume",
    response_model=AudioZone,
    dependencies=[Depends(require_mutation_access)],
)
async def set_zone_volume(
    request: Request, zone_id: str, payload: ZoneVolumeRequest
) -> AudioZone:
    """Persist and apply per-zone volume (0..100)."""

    return await _manager(request).set_volume(zone_id, payload.volume)


@router.post(
    "/{zone_id}/offset",
    response_model=AudioZone,
    dependencies=[Depends(require_mutation_access)],
)
async def set_zone_offset(
    request: Request, zone_id: str, payload: ZoneOffsetRequest
) -> AudioZone:
    """Persist the karaoke delay offset for one zone (stored, applied by UI)."""

    return await _manager(request).set_offset(zone_id, payload.offset_ms)
