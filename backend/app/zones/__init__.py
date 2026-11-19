"""Audio zones: per-zone persistence, PipeWire routing and switching (P7)."""

from __future__ import annotations

from app.zones.manager import (
    ZoneManager,
    create_zone_manager,
)
from app.zones.repository import (
    ACTIVE_ZONE_SETTING_KEY,
    DEFAULT_ZONE_ID,
    ZONE_OFFSET_MAX_MS,
    ZONE_OFFSET_MIN_MS,
    ZoneRepository,
    row_to_zone,
)
from app.zones.router import (
    CommandResult,
    MockZoneRouter,
    PipeWireRouter,
    SinkInfo,
    ZoneRouter,
    create_zone_router,
)

__all__ = [
    "ACTIVE_ZONE_SETTING_KEY",
    "DEFAULT_ZONE_ID",
    "ZONE_OFFSET_MAX_MS",
    "ZONE_OFFSET_MIN_MS",
    "CommandResult",
    "MockZoneRouter",
    "PipeWireRouter",
    "SinkInfo",
    "ZoneManager",
    "ZoneRepository",
    "ZoneRouter",
    "create_zone_manager",
    "create_zone_router",
    "row_to_zone",
]
