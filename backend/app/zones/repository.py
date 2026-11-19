"""Persistent audio zones (``audio_zones`` table) and the active-zone pointer.

The schema is frozen (``0001_init.sql``); this repository never runs DDL. The
database stores no ``name`` or ``active`` column, so the display name is derived
from the stable zone id and the single active zone is persisted as the
``audio.active_zone`` key/value setting. That keeps the schema untouched while
surviving restarts.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, cast

from app.core.models import AudioZone
from app.database.connection import Database, Row

#: ``settings`` key holding the id of the single active zone (MVP: one at a time).
ACTIVE_ZONE_SETTING_KEY = "audio.active_zone"
DEFAULT_ZONE_ID = "local"

#: Karaoke delay offset is stored per zone; the UI only needs fine adjustment.
ZONE_OFFSET_MIN_MS = -5000
ZONE_OFFSET_MAX_MS = 5000

ZoneKind = Literal["local_sink", "browser", "bt"]

#: Rows seeded on first use (id, kind, display name). The BT row is a placeholder
#: until a device is paired/connected; it then receives a real A2DP sink name.
DEFAULT_ZONES: tuple[tuple[str, ZoneKind, str], ...] = (
    ("local", "local_sink", "Local output"),
    ("browser", "browser", "Browser"),
    ("bt", "bt", "Bluetooth"),
)

_DEFAULT_NAMES = {zone_id: name for zone_id, _kind, name in DEFAULT_ZONES}
_ZONE_COLUMNS = "id, kind, sink_name, volume, delay_offset_ms"
_ORDER_BY = (
    "ORDER BY CASE kind WHEN 'local_sink' THEN 0 WHEN 'browser' THEN 1 ELSE 2 END, id"
)
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _zone_available(kind: ZoneKind, sink_name: str | None) -> bool:
    """A BT zone only exists once an A2DP sink is registered.

    Local/browser zones are virtual or always present; whether the local sink is
    actually reachable is a router concern surfaced by ``ZoneManager``.
    """

    return kind != "bt" or bool(sink_name)


def row_to_zone(row: Row, *, active_id: str | None = None) -> AudioZone:
    """Map one ``audio_zones`` row to the public DTO."""

    zone_id = str(row["id"])
    kind = cast(ZoneKind, str(row["kind"]))
    sink_name = row["sink_name"]
    return AudioZone(
        id=zone_id,
        kind=kind,
        name=_DEFAULT_NAMES.get(zone_id, sink_name or zone_id),
        sink_name=sink_name,
        volume=int(row["volume"]),
        offset_ms=int(row["delay_offset_ms"]),
        active=active_id == zone_id,
        available=_zone_available(kind, sink_name),
    )


class ZoneRepository:
    """CRUD for audio zones; the only place that knows the zone SQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def ensure_defaults(self) -> None:
        """Seed local/browser/bt placeholder rows and the active-zone setting.

        Idempotent: existing rows and a user's active zone are never overwritten.
        """

        async with self._db.transaction() as conn:
            for zone_id, kind, _name in DEFAULT_ZONES:
                await conn.execute(
                    f"""
                    INSERT OR IGNORE INTO audio_zones
                        (id, kind, sink_name, volume, delay_offset_ms, last_seen)
                    VALUES (?, ?, NULL, 100, 0, {_NOW})
                    """,
                    (zone_id, kind),
                )
            await conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (ACTIVE_ZONE_SETTING_KEY, json.dumps(DEFAULT_ZONE_ID)),
            )

    async def active_zone_id(self) -> str | None:
        row = await self._db.fetchone(
            "SELECT value FROM settings WHERE key = ?", (ACTIVE_ZONE_SETTING_KEY,)
        )
        if row is None:
            return None
        try:
            value: Any = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        return value if isinstance(value, str) and value else None

    async def list(self) -> list[AudioZone]:
        rows = await self._db.fetchall(
            f"SELECT {_ZONE_COLUMNS} FROM audio_zones {_ORDER_BY}"
        )
        active_id = await self.active_zone_id()
        return [row_to_zone(row, active_id=active_id) for row in rows]

    async def get(self, zone_id: str) -> AudioZone | None:
        row = await self._db.fetchone(
            f"SELECT {_ZONE_COLUMNS} FROM audio_zones WHERE id = ?", (zone_id,)
        )
        if row is None:
            return None
        return row_to_zone(row, active_id=await self.active_zone_id())

    async def set_volume(self, zone_id: str, volume: int) -> AudioZone | None:
        if not 0 <= volume <= 100:
            raise ValueError("volume must be between 0 and 100")
        return await self._update(zone_id, "volume = ?", (volume,))

    async def set_offset(self, zone_id: str, offset_ms: int) -> AudioZone | None:
        if not ZONE_OFFSET_MIN_MS <= offset_ms <= ZONE_OFFSET_MAX_MS:
            raise ValueError(
                f"offset_ms must be between {ZONE_OFFSET_MIN_MS} and {ZONE_OFFSET_MAX_MS}"
            )
        return await self._update(zone_id, "delay_offset_ms = ?", (offset_ms,))

    async def set_sink_name(self, zone_id: str, sink_name: str | None) -> AudioZone | None:
        return await self._update(zone_id, "sink_name = ?", (sink_name,))

    async def mark_active(self, zone_id: str) -> AudioZone | None:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE audio_zones SET last_seen = {_NOW}, updated_at = {_NOW} "
                "WHERE id = ?",
                (zone_id,),
            )
            changed = cursor.rowcount
            await cursor.close()
            if not changed:
                return None
            await conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (ACTIVE_ZONE_SETTING_KEY, json.dumps(zone_id)),
            )
        return await self.get(zone_id)

    async def touch(self, zone_id: str) -> bool:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE audio_zones SET last_seen = {_NOW}, updated_at = {_NOW} "
                "WHERE id = ?",
                (zone_id,),
            )
            changed = cursor.rowcount
            await cursor.close()
        return bool(changed)

    async def _update(
        self, zone_id: str, assignment: str, parameters: Sequence[Any]
    ) -> AudioZone | None:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE audio_zones SET {assignment}, updated_at = {_NOW} "
                "WHERE id = ?",
                (*parameters, zone_id),
            )
            changed = cursor.rowcount
            await cursor.close()
        if not changed:
            return None
        return await self.get(zone_id)


__all__ = [
    "ACTIVE_ZONE_SETTING_KEY",
    "DEFAULT_ZONES",
    "DEFAULT_ZONE_ID",
    "ZONE_OFFSET_MAX_MS",
    "ZONE_OFFSET_MIN_MS",
    "ZoneKind",
    "ZoneRepository",
    "row_to_zone",
]
