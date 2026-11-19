"""SQLite repository for cached lyrics (``lyrics`` table, migration 0001).

One row per ``(track_id, language, format)`` because the schema makes that
triple UNIQUE.  The repository never parses LRC: ``content`` is stored and
returned verbatim, so the parser stays the only authority on timing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import aiosqlite

from app.core.timeutils import utc_now_iso
from app.database.connection import Database, Row
from app.errors import NotFoundError
from app.lyrics.parser import LyricsFormat

LYRICS_FORMATS: tuple[LyricsFormat, ...] = ("lrc", "plain")

# The UI offset is a nudge, not a re-sync: keep it within one minute.
OFFSET_LIMIT_MS = 60_000

_RECORD_COLUMNS = "track_id, language, format, content, source, offset_ms, updated_at"

# lrc always wins over plain; ties fall back to the freshest row (id keeps the
# order deterministic when updated_at has millisecond precision).
_LRC_FIRST = "CASE format WHEN 'lrc' THEN 0 ELSE 1 END"


@dataclass(frozen=True)
class LyricsRecord:
    """One cached lyrics variant as stored in SQLite."""

    track_id: int
    language: str
    format: LyricsFormat
    content: str
    source: str | None
    offset_ms: int
    updated_at: str


def normalize_language(language: str | None) -> str:
    """Canonical cache key: trimmed, lower-case, ``und`` when empty."""

    if language is None:
        return "und"
    normalized = language.strip().lower()
    return normalized or "und"


def clamp_offset(offset_ms: int) -> int:
    """Clamp a user offset to ``[-60000, 60000]`` milliseconds."""

    return max(-OFFSET_LIMIT_MS, min(OFFSET_LIMIT_MS, int(offset_ms)))


def _record_from_row(row: Row) -> LyricsRecord:
    return LyricsRecord(
        track_id=int(row["track_id"]),
        language=row["language"],
        format=cast(LyricsFormat, row["format"]),
        content=row["content"],
        source=row["source"],
        offset_ms=int(row["offset_ms"]),
        updated_at=row["updated_at"],
    )


async def _fetch_record(
    conn: aiosqlite.Connection,
    where: str,
    parameters: Sequence[object],
) -> Row | None:
    cursor = await conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM lyrics WHERE {where} LIMIT 1", parameters
    )
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


class LyricsRepository:
    """CRUD over the frozen ``lyrics`` table; the only module writing its SQL."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ reads

    async def get(self, track_id: int, language: str | None = None) -> LyricsRecord | None:
        """Best cached variant for a track.

        LRC beats plain text regardless of insertion order.  A language filter
        is exact (normalised to lower case); ``language=None`` considers every
        language and prefers the most recently updated row.
        """

        if language is None:
            row = await self._db.fetchone(
                f"""
                SELECT {_RECORD_COLUMNS} FROM lyrics
                WHERE track_id = ?
                ORDER BY {_LRC_FIRST}, updated_at DESC, id DESC
                LIMIT 1
                """,
                (track_id,),
            )
        else:
            row = await self._db.fetchone(
                f"""
                SELECT {_RECORD_COLUMNS} FROM lyrics
                WHERE track_id = ? AND language = ?
                ORDER BY {_LRC_FIRST}, updated_at DESC, id DESC
                LIMIT 1
                """,
                (track_id, normalize_language(language)),
            )
        return _record_from_row(row) if row is not None else None

    # ----------------------------------------------------------------- writes

    async def save(
        self,
        track_id: int,
        *,
        language: str = "und",
        format: LyricsFormat,
        content: str,
        source: str | None = None,
    ) -> LyricsRecord:
        """Insert or replace one lyrics variant.

        Upsert keyed by ``UNIQUE(track_id, language, format)``.  ``offset_ms``
        is intentionally not overwritten: a user offset survives re-caching the
        same text.  Raises ``NotFoundError`` for an unknown track (FK) and
        ``ValueError`` for a format outside the schema CHECK.
        """

        if format not in LYRICS_FORMATS:
            raise ValueError(f"invalid lyrics format: {format!r}")
        normalized_language = normalize_language(language)
        timestamp = utc_now_iso()
        try:
            async with self._db.transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO lyrics
                        (track_id, language, format, content, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (track_id, language, format) DO UPDATE SET
                        content = excluded.content,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (track_id, normalized_language, format, content, source, timestamp),
                )
                row = await _fetch_record(
                    conn,
                    "track_id = ? AND language = ? AND format = ?",
                    (track_id, normalized_language, format),
                )
        except aiosqlite.IntegrityError as exc:
            # Unknown track: the foreign key is the authority, no pre-check race.
            raise NotFoundError("Track not found", details={"track_id": track_id}) from exc
        if row is None:  # pragma: no cover - defensive
            raise NotFoundError("Lyrics not found", details={"track_id": track_id})
        return _record_from_row(row)

    async def set_offset(self, track_id: int, offset_ms: int) -> bool:
        """Store the UI offset for every cached variant of a track.

        The offset is clamped to +/-60 s.  Returns ``False`` when the track has
        no cached lyrics (the caller decides whether that is an error), so the
        method never raises for a missing row.
        """

        value = clamp_offset(offset_ms)
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE lyrics SET offset_ms = ?, updated_at = ? WHERE track_id = ?",
                (value, utc_now_iso(), track_id),
            )
            changed = (cursor.rowcount or 0) > 0
            await cursor.close()
        return changed

    async def delete(self, track_id: int) -> bool:
        """Drop every cached variant for a track; ``False`` when none existed."""

        async with self._db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM lyrics WHERE track_id = ?", (track_id,))
            deleted = (cursor.rowcount or 0) > 0
            await cursor.close()
        return deleted


__all__ = [
    "LYRICS_FORMATS",
    "OFFSET_LIMIT_MS",
    "LyricsRecord",
    "LyricsRepository",
    "clamp_offset",
    "normalize_language",
]
