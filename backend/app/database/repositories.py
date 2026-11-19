"""Repositories: the only place that knows SQL."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite

from app.core.availability import compute_availability
from app.core.models import (
    AlbumDetail,
    AlbumRef,
    AlbumSummary,
    ArtistDetail,
    ArtistRef,
    ArtistSummary,
    HistoryEntry,
    HistoryPage,
    ImportOutcome,
    LocalMediaMetadata,
    PlayerStatus,
    QueueItem,
    Track,
)
from app.database.connection import Database, Row
from app.errors import QueueChangedError

ARTIST_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"
# Container/encoder padding differs between MP3/FLAC/AAC for the same recording.
DEDUP_DURATION_TOLERANCE_MS = 1500
_FTS_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def artwork_url(cover_key: str | None, size: int = 300) -> str | None:
    if not cover_key:
        return None
    if cover_key.startswith(("http://", "https://")):
        return cover_key
    return f"/api/v1/artwork/{quote(cover_key)}?size={size}"


def _fts_expression(value: str) -> str | None:
    tokens = _FTS_TOKEN.findall(value)
    if not tokens:
        return None
    return " ".join(f"{token}*" for token in tokens)


TRACK_COLUMNS = """
    t.id AS track_id,
    t.title AS track_title,
    t.duration_ms AS track_duration_ms,
    t.genre AS track_genre,
    t.label AS track_label,
    t.has_local_file,
    t.has_online_source,
    t.file_missing,
    t.source_unavailable,
    al.id AS album_id,
    al.title AS album_title,
    al.year AS album_year,
    al.cover_path AS album_cover_path,
    (
        SELECT s.name
        FROM track_sources ts
        JOIN sources s ON s.id = ts.source_id
        WHERE ts.track_id = t.id
        ORDER BY CASE WHEN s.name = 'yandex' THEN 0 ELSE 1 END, ts.id
        LIMIT 1
    ) AS track_source,
    (
        SELECT ts.external_id
        FROM track_sources ts
        JOIN sources s ON s.id = ts.source_id
        WHERE ts.track_id = t.id
        ORDER BY CASE WHEN s.name = 'yandex' THEN 0 ELSE 1 END, ts.id
        LIMIT 1
    ) AS track_external_id,
    (
        SELECT group_concat(artist_id || char(31) || artist_name, char(30))
        FROM (
            SELECT ta.artist_id AS artist_id, a.name AS artist_name
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id = t.id
            ORDER BY ta.position, a.id
        )
    ) AS artists_blob
"""

TRACK_FROM = """
    FROM tracks t
    LEFT JOIN albums al ON al.id = t.album_id
"""

TRACK_ALIVE = "t.deleted_at IS NULL"

QUEUE_COLUMNS = """
    q.id AS item_id,
    q.position AS item_position,
    q.source_playlist_id,
""" + TRACK_COLUMNS

QUEUE_FROM = """
    FROM queue_items q
    JOIN tracks t ON t.id = q.track_id
    LEFT JOIN albums al ON al.id = t.album_id
"""

AVAILABILITY_FILTERS = {
    "missing": "t.file_missing = 1",
    "offline": "t.file_missing = 0 AND t.has_local_file = 1",
    "unavailable": (
        "t.file_missing = 0 AND t.has_local_file = 0 "
        "AND t.has_online_source = 1 AND t.source_unavailable = 1"
    ),
    "online": (
        "t.file_missing = 0 AND t.has_local_file = 0 "
        "AND t.has_online_source = 1 AND t.source_unavailable = 0"
    ),
    "metadata_only": (
        "t.file_missing = 0 AND t.has_local_file = 0 AND t.has_online_source = 0"
    ),
}


def row_to_track(row: Row) -> Track:
    artists: list[ArtistRef] = []
    blob = row["artists_blob"] or ""
    for chunk in blob.split(ARTIST_SEPARATOR):
        if not chunk:
            continue
        artist_id, _, artist_name = chunk.partition(FIELD_SEPARATOR)
        if not artist_name:
            continue
        artists.append(ArtistRef(id=int(artist_id), name=artist_name))

    album = None
    if row["album_id"] is not None:
        album = AlbumRef(
            id=int(row["album_id"]),
            title=row["album_title"] or "",
            year=row["album_year"],
            cover_path=row["album_cover_path"],
        )

    has_local_file = bool(row["has_local_file"])
    has_online_source = bool(row["has_online_source"])
    file_missing = bool(row["file_missing"])
    source_unavailable = bool(row["source_unavailable"])

    return Track(
        id=int(row["track_id"]),
        title=row["track_title"],
        artists=artists,
        album=album,
        duration_ms=row["track_duration_ms"],
        genre=row["track_genre"],
        label=row["track_label"],
        availability=compute_availability(
            has_local_file=has_local_file,
            has_online_source=has_online_source,
            file_missing=file_missing,
            source_unavailable=source_unavailable,
        ),
        has_local_file=has_local_file,
        has_online_source=has_online_source,
        file_missing=file_missing,
        source_unavailable=source_unavailable,
        cover_url=artwork_url(row["album_cover_path"]),
        source=row["track_source"],
        external_id=row["track_external_id"],
    )


def row_to_queue_item(row: Row) -> QueueItem:
    return QueueItem(
        id=int(row["item_id"]),
        position=int(row["item_position"]),
        source_playlist_id=row["source_playlist_id"],
        track=row_to_track(row),
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _fetchall(
    conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> list[Row]:
    cursor = await conn.execute(sql, parameters)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


class TrackRepository:
    """Read access to the unified track catalog."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, track_id: int) -> Track | None:
        row = await self._db.fetchone(
            f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
            f"WHERE t.id = ? AND {TRACK_ALIVE}",
            (track_id,),
        )
        return row_to_track(row) if row is not None else None

    async def exists(self, track_id: int) -> bool:
        return (
            await self._db.scalar(
                f"SELECT 1 FROM tracks t WHERE t.id = ? AND {TRACK_ALIVE}",
                (track_id,),
            )
            is not None
        )

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        availability: str = "all",
    ) -> tuple[list[Track], int]:
        conditions: list[str] = [TRACK_ALIVE]
        parameters: list[Any] = []
        if search and search.strip():
            fts = _fts_expression(search)
            if fts:
                conditions.append(
                    "t.id IN (SELECT rowid FROM tracks_fts WHERE tracks_fts MATCH ?)"
                )
                parameters.append(fts)
            else:
                term = f"%{_escape_like(search.strip())}%"
                conditions.append(
                    "(t.title LIKE ? ESCAPE '\\' OR EXISTS ("
                    "SELECT 1 FROM track_artists ta JOIN artists a ON a.id = ta.artist_id "
                    "WHERE ta.track_id = t.id AND a.name LIKE ? ESCAPE '\\'))"
                )
                parameters.extend([term, term])
        filter_sql = AVAILABILITY_FILTERS.get(availability)
        if filter_sql:
            conditions.append(f"({filter_sql})")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = await self._db.fetchall(
            f"SELECT {TRACK_COLUMNS} {TRACK_FROM} {where} "
            "ORDER BY t.title COLLATE NOCASE, t.id LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        )
        total_row = await self._db.fetchone(
            f"SELECT COUNT(*) {TRACK_FROM} {where}", parameters
        )
        total = int(total_row[0]) if total_row is not None else 0
        return [row_to_track(row) for row in rows], total

    async def count(self) -> int:
        return int(
            await self._db.scalar(
                f"SELECT COUNT(*) FROM tracks t WHERE {TRACK_ALIVE}"
            )
            or 0
        )

    async def get_media_path(self, track_id: int) -> Path | None:
        row = await self._db.fetchone(
            """
            SELECT path FROM media_files
            WHERE track_id = ? AND role IN ('library', 'download')
            ORDER BY CASE role WHEN 'download' THEN 0 ELSE 1 END, id
            LIMIT 1
            """,
            (track_id,),
        )
        return Path(row["path"]) if row is not None else None


class LibraryRepository:
    """P2 local library: import, search index, albums/artists, verification."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------- search index

    async def rebuild_search_index_if_needed(self) -> int:
        tracks = int(
            await self._db.scalar(
                f"SELECT COUNT(*) FROM tracks t WHERE {TRACK_ALIVE}"
            )
            or 0
        )
        indexed = int(await self._db.scalar("SELECT COUNT(*) FROM tracks_fts") or 0)
        if tracks != indexed:
            await self.rebuild_search_index()
            return tracks
        return 0

    async def rebuild_search_index(self) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM tracks_fts")
            rows = await _fetchall(
                conn,
                f"SELECT {TRACK_COLUMNS} {TRACK_FROM} WHERE {TRACK_ALIVE} "
                "ORDER BY t.id",
            )
            for row in rows:
                await self._index_row(conn, row)

    @staticmethod
    def _artist_names_from_blob(blob: str | None) -> str:
        names: list[str] = []
        for chunk in (blob or "").split(ARTIST_SEPARATOR):
            if not chunk:
                continue
            _, _, name = chunk.partition(FIELD_SEPARATOR)
            if name:
                names.append(name)
        return " ".join(names)

    async def _index_row(self, conn: aiosqlite.Connection, row: Row) -> None:
        await conn.execute("DELETE FROM tracks_fts WHERE rowid = ?", (row["track_id"],))
        await conn.execute(
            "INSERT INTO tracks_fts (rowid, title, artists, album) VALUES (?, ?, ?, ?)",
            (
                row["track_id"],
                row["track_title"],
                self._artist_names_from_blob(row["artists_blob"]),
                row["album_title"] or "",
            ),
        )

    async def index_track(self, track_id: int, conn: aiosqlite.Connection | None = None) -> None:
        if conn is None:
            row = await self._db.fetchone(
                f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
                f"WHERE t.id = ? AND {TRACK_ALIVE}",
                (track_id,),
            )
            if row is not None:
                async with self._db.transaction() as own:
                    await self._index_row(own, row)
            return
        row = await _fetchone(
            conn,
            f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
            f"WHERE t.id = ? AND {TRACK_ALIVE}",
            (track_id,),
        )
        if row is not None:
            await self._index_row(conn, row)

    # ------------------------------------------------------------- albums/artists

    async def list_albums(
        self, *, limit: int = 50, offset: int = 0, search: str | None = None
    ) -> tuple[list[AlbumSummary], int]:
        where = ""
        parameters: list[Any] = []
        if search and search.strip():
            term = f"%{_escape_like(search.strip())}%"
            where = " WHERE (al.title LIKE ? ESCAPE '\\' OR ar.name LIKE ? ESCAPE '\\')"
            parameters = [term, term]
        rows = await self._db.fetchall(
            f"""
            SELECT al.id, al.title, al.year, al.cover_path,
                   ar.id AS artist_id, ar.name AS artist_name,
                   (SELECT COUNT(*) FROM tracks t
                    WHERE t.album_id = al.id AND t.deleted_at IS NULL) AS track_count
            FROM albums al
            LEFT JOIN artists ar ON ar.id = al.artist_id
            {where}
            ORDER BY al.title COLLATE NOCASE, al.id
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        )
        total_row = await self._db.fetchone(
            f"SELECT COUNT(*) FROM albums al LEFT JOIN artists ar ON ar.id = al.artist_id {where}",
            parameters,
        )
        items = [
            AlbumSummary(
                id=int(row["id"]),
                title=row["title"],
                year=row["year"],
                artist=(
                    ArtistRef(id=int(row["artist_id"]), name=row["artist_name"])
                    if row["artist_id"] is not None
                    else None
                ),
                cover_url=artwork_url(row["cover_path"]),
                track_count=int(row["track_count"]),
            )
            for row in rows
        ]
        return items, int(total_row[0]) if total_row is not None else 0

    async def list_artists(
        self, *, limit: int = 50, offset: int = 0, search: str | None = None
    ) -> tuple[list[ArtistSummary], int]:
        where = ""
        parameters: list[Any] = []
        if search and search.strip():
            where = " WHERE a.name LIKE ? ESCAPE '\\'"
            parameters = [f"%{_escape_like(search.strip())}%"]
        rows = await self._db.fetchall(
            f"""
            SELECT a.id, a.name, a.cover_path,
                   COUNT(DISTINCT t.id) AS track_count,
                   COUNT(DISTINCT t.album_id) AS album_count
            FROM artists a
            LEFT JOIN track_artists ta ON ta.artist_id = a.id
            LEFT JOIN tracks t ON t.id = ta.track_id AND t.deleted_at IS NULL
            {where}
            GROUP BY a.id
            ORDER BY a.name COLLATE NOCASE, a.id
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        )
        total_row = await self._db.fetchone(
            f"SELECT COUNT(*) FROM artists a {where}", parameters
        )
        items = [
            ArtistSummary(
                id=int(row["id"]),
                name=row["name"],
                cover_url=artwork_url(row["cover_path"]),
                track_count=int(row["track_count"]),
                album_count=int(row["album_count"]),
            )
            for row in rows
        ]
        return items, int(total_row[0]) if total_row is not None else 0

    # ------------------------------------------------------------------- import

    async def album_detail(self, album_id: int) -> AlbumDetail | None:
        row = await self._db.fetchone(
            """
            SELECT al.id, al.title, al.year, al.cover_path,
                   ar.id AS artist_id, ar.name AS artist_name,
                   (SELECT COUNT(*) FROM tracks t
                    WHERE t.album_id = al.id AND t.deleted_at IS NULL) AS track_count
            FROM albums al
            LEFT JOIN artists ar ON ar.id = al.artist_id
            WHERE al.id = ?
            """,
            (album_id,),
        )
        if row is None:
            return None
        track_rows = await self._db.fetchall(
            f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
            f"WHERE t.album_id = ? AND {TRACK_ALIVE} "
            "ORDER BY t.disc_number, t.track_number, t.title COLLATE NOCASE, t.id",
            (album_id,),
        )
        return AlbumDetail(
            album=AlbumSummary(
                id=int(row["id"]),
                title=row["title"],
                year=row["year"],
                artist=(
                    ArtistRef(id=int(row["artist_id"]), name=row["artist_name"])
                    if row["artist_id"] is not None
                    else None
                ),
                cover_url=artwork_url(row["cover_path"]),
                track_count=int(row["track_count"]),
            ),
            tracks=[row_to_track(track_row) for track_row in track_rows],
        )

    async def artist_detail(self, artist_id: int) -> ArtistDetail | None:
        row = await self._db.fetchone(
            """
            SELECT a.id, a.name, a.cover_path,
                   COUNT(DISTINCT t.id) AS track_count,
                   COUNT(DISTINCT t.album_id) AS album_count
            FROM artists a
            LEFT JOIN track_artists ta ON ta.artist_id = a.id
            LEFT JOIN tracks t ON t.id = ta.track_id AND t.deleted_at IS NULL
            WHERE a.id = ?
            GROUP BY a.id
            """,
            (artist_id,),
        )
        if row is None:
            return None
        track_rows = await self._db.fetchall(
            f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
            "WHERE t.id IN (SELECT track_id FROM track_artists WHERE artist_id = ?) "
            f"AND {TRACK_ALIVE} "
            "ORDER BY t.title COLLATE NOCASE, t.id",
            (artist_id,),
        )
        return ArtistDetail(
            artist=ArtistSummary(
                id=int(row["id"]),
                name=row["name"],
                cover_url=artwork_url(row["cover_path"]),
                track_count=int(row["track_count"]),
                album_count=int(row["album_count"]),
            ),
            tracks=[row_to_track(track_row) for track_row in track_rows],
        )

    async def import_local(
        self, metadata: LocalMediaMetadata, source_id: int
    ) -> ImportOutcome:
        async with self._db.transaction() as conn:
            external_id = f"local:{metadata.relative_path}"
            existing = await _fetchone(
                conn,
                "SELECT track_id FROM track_sources WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            )
            if existing is not None:
                track_id = int(existing["track_id"])
                media_file_id, media_status = await self._add_media_file(
                    conn, track_id, metadata
                )
                album_id, track_updated = await self._update_existing_track(
                    conn, track_id, media_file_id, metadata
                )
                return ImportOutcome(
                    track_id=track_id,
                    media_file_id=media_file_id,
                    status=(
                        "updated"
                        if track_updated or media_status != "skipped"
                        else "skipped"
                    ),
                    album_id=album_id,
                )

            duplicate_id = await self._find_duplicate(conn, metadata)
            if duplicate_id is not None:
                media_file_id, media_status = await self._add_media_file(
                    conn, duplicate_id, metadata
                )
                await conn.execute(
                    """
                    INSERT OR IGNORE INTO track_sources
                        (track_id, source_id, external_id, availability, last_checked_at)
                    VALUES (?, ?, ?, 'available', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    (duplicate_id, source_id, external_id),
                )
                return ImportOutcome(
                    track_id=duplicate_id,
                    media_file_id=media_file_id,
                    status="updated" if media_status == "created" else "skipped",
                )

            artist_ids: list[int] = []
            for name in metadata.artists:
                artist_ids.append(await self._get_or_create_artist(conn, name))
            if not artist_ids:
                artist_ids.append(await self._get_or_create_artist(conn, "Unknown Artist"))
            album_artist_name = metadata.albumartist or (
                metadata.artists[0] if metadata.artists else "Unknown Artist"
            )
            album_artist_id = await self._get_or_create_artist(conn, album_artist_name)
            album_id = await self._get_or_create_album(
                conn,
                metadata.album,
                album_artist_id,
                metadata.year,
            )
            cursor = await conn.execute(
                """
                INSERT INTO tracks
                    (title, album_id, duration_ms, genre, label, year, track_number,
                     disc_number, has_local_file, has_online_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    metadata.title,
                    album_id,
                    metadata.duration_ms,
                    metadata.genre,
                    metadata.label,
                    metadata.year,
                    metadata.track_number,
                    metadata.disc_number,
                ),
            )
            track_id = int(cursor.lastrowid or 0)
            await cursor.close()
            for position, artist_id in enumerate(artist_ids):
                await conn.execute(
                    "INSERT OR IGNORE INTO track_artists (track_id, artist_id, position) "
                    "VALUES (?, ?, ?)",
                    (track_id, artist_id, position),
                )
            await conn.execute(
                """
                INSERT INTO track_sources
                    (track_id, source_id, external_id, availability, last_checked_at)
                VALUES (?, ?, ?, 'available', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (track_id, source_id, external_id),
            )
            media_file_id, _ = await self._add_media_file(conn, track_id, metadata)
            await self.index_track(track_id, conn)
            return ImportOutcome(
                track_id=track_id,
                media_file_id=media_file_id,
                status="created",
                album_id=album_id,
            )

    async def _update_existing_track(
        self,
        conn: aiosqlite.Connection,
        track_id: int,
        media_file_id: int,
        metadata: LocalMediaMetadata,
    ) -> tuple[int | None, bool]:
        """Refresh track-level tags from an existing track's primary file.

        Several containers may share one track (MP3 + FLAC). The oldest media
        file owns the track-level tags: sibling files can differ by a few
        milliseconds, and letting every re-import overwrite the track would make
        repeated scans non-idempotent. Non-primary files still update their own
        media_files row.
        """

        older = await _fetchone(
            conn,
            "SELECT 1 FROM media_files WHERE track_id = ? AND id < ? LIMIT 1",
            (track_id, media_file_id),
        )
        if older is not None:
            row = await _fetchone(conn, "SELECT album_id FROM tracks WHERE id = ?", (track_id,))
            return (int(row["album_id"]) if row and row["album_id"] is not None else None), False

        album_artist_name = metadata.albumartist or (
            metadata.artists[0] if metadata.artists else "Unknown Artist"
        )
        album_artist_id = await self._get_or_create_artist(conn, album_artist_name)
        album_id = await self._get_or_create_album(
            conn,
            metadata.album,
            album_artist_id,
            metadata.year,
        )
        current = await _fetchone(
            conn,
            "SELECT title, duration_ms, genre, label, year, track_number, disc_number, "
            "album_id FROM tracks WHERE id = ?",
            (track_id,),
        )
        if current is None:  # pragma: no cover - defensive
            return album_id, False
        artist_rows = await _fetchall(
            conn,
            """
            SELECT a.name AS name
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id = ?
            ORDER BY ta.position, a.id
            """,
            (track_id,),
        )
        current_artists = [row["name"] for row in artist_rows]
        desired_artists = metadata.artists or ["Unknown Artist"]
        desired = (
            metadata.title,
            metadata.duration_ms,
            metadata.genre,
            metadata.label,
            metadata.year,
            metadata.track_number,
            metadata.disc_number,
            album_id,
        )
        existing = (
            current["title"],
            current["duration_ms"],
            current["genre"],
            current["label"],
            current["year"],
            current["track_number"],
            current["disc_number"],
            current["album_id"],
        )
        if tuple(existing) == desired and current_artists == desired_artists:
            return album_id, False
        await conn.execute(
            """
            UPDATE tracks
            SET title = ?, duration_ms = ?, genre = ?, label = ?, year = ?,
                track_number = ?, disc_number = ?, album_id = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (*desired, track_id),
        )
        if current_artists != desired_artists:
            await conn.execute("DELETE FROM track_artists WHERE track_id = ?", (track_id,))
            for position, name in enumerate(desired_artists):
                artist_id = await self._get_or_create_artist(conn, name)
                await conn.execute(
                    "INSERT INTO track_artists (track_id, artist_id, position) "
                    "VALUES (?, ?, ?)",
                    (track_id, artist_id, position),
                )
        await self.index_track(track_id, conn)
        return album_id, True

    async def _add_media_file(
        self, conn: aiosqlite.Connection, track_id: int, metadata: LocalMediaMetadata
    ) -> tuple[int, str]:
        existing = await _fetchone(
            conn,
            "SELECT id, track_id, format, size_bytes, duration_ms, bitrate, sample_rate, "
            "channels FROM media_files WHERE path = ?",
            (str(metadata.path),),
        )
        if existing is not None:
            if int(existing["track_id"]) != track_id:
                return int(existing["id"]), "skipped"
            desired = (
                metadata.format,
                metadata.size_bytes,
                metadata.duration_ms,
                metadata.bitrate,
                metadata.sample_rate,
                metadata.channels,
            )
            current = (
                existing["format"],
                existing["size_bytes"],
                existing["duration_ms"],
                existing["bitrate"],
                existing["sample_rate"],
                existing["channels"],
            )
            if tuple(current) == desired:
                return int(existing["id"]), "skipped"
            media_file_id = int(existing["id"])
            await conn.execute(
                """
                UPDATE media_files
                SET format = ?, size_bytes = ?, duration_ms = ?, bitrate = ?,
                    sample_rate = ?, channels = ?
                WHERE id = ?
                """,
                (*desired, media_file_id),
            )
            if (
                existing["size_bytes"] != metadata.size_bytes
                or existing["duration_ms"] != metadata.duration_ms
            ):
                # The bytes changed: force the importer to verify this file again.
                await conn.execute(
                    "UPDATE media_files SET checksum = NULL, verified_at = NULL WHERE id = ?",
                    (media_file_id,),
                )
            return media_file_id, "updated"
        cursor = await conn.execute(
            """
            INSERT INTO media_files
                (track_id, role, path, format, bitrate, sample_rate, channels,
                 size_bytes, duration_ms)
            VALUES (?, 'library', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                track_id,
                str(metadata.path),
                metadata.format,
                metadata.bitrate,
                metadata.sample_rate,
                metadata.channels,
                metadata.size_bytes,
                metadata.duration_ms,
            ),
        )
        media_file_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return media_file_id, "created"

    async def _find_duplicate(
        self, conn: aiosqlite.Connection, metadata: LocalMediaMetadata
    ) -> int | None:
        if metadata.duration_ms is None:
            return None
        target_artists = sorted(" ".join(a.casefold().split()) for a in metadata.artists)
        # Local imports store a missing album tag as "Unknown Album"; normalize
        # both sides so untagged files from different paths still merge.
        target_album = " ".join((metadata.album or "Unknown Album").casefold().split())
        target_title = " ".join(metadata.title.casefold().split())
        rows = await _fetchall(
            conn,
            f"""
            SELECT {TRACK_COLUMNS} {TRACK_FROM}
            WHERE t.duration_ms BETWEEN ? AND ?
            """,
            (
                metadata.duration_ms - DEDUP_DURATION_TOLERANCE_MS,
                metadata.duration_ms + DEDUP_DURATION_TOLERANCE_MS,
            ),
        )
        for row in rows:
            if " ".join(row["track_title"].casefold().split()) != target_title:
                continue
            row_album = " ".join(
                (row["album_title"] or "Unknown Album").casefold().split()
            )
            if row_album != target_album:
                continue
            candidate = row_to_track(row)
            candidate_artists = sorted(
                " ".join(artist.name.casefold().split()) for artist in candidate.artists
            )
            if candidate_artists == target_artists:
                return candidate.id
        return None

    async def _get_or_create_artist(self, conn: aiosqlite.Connection, name: str) -> int:
        row = await _fetchone(
            conn, "SELECT id FROM artists WHERE name = ? ORDER BY id LIMIT 1", (name,)
        )
        if row is not None:
            return int(row["id"])
        cursor = await conn.execute("INSERT INTO artists (name) VALUES (?)", (name,))
        artist_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return artist_id

    async def _get_or_create_album(
        self,
        conn: aiosqlite.Connection,
        title: str | None,
        artist_id: int,
        year: int | None,
    ) -> int:
        album_title = title or "Unknown Album"
        row = await _fetchone(
            conn,
            "SELECT id FROM albums WHERE title = ? AND artist_id = ? ORDER BY id LIMIT 1",
            (album_title, artist_id),
        )
        if row is not None:
            return int(row["id"])
        cursor = await conn.execute(
            "INSERT INTO albums (title, artist_id, year) VALUES (?, ?, ?)",
            (album_title, artist_id, year),
        )
        album_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return album_id

    async def get_or_create_source(self, name: str) -> int:
        async with self._db.transaction() as conn:
            await conn.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (name,))
            row = await _fetchone(conn, "SELECT id FROM sources WHERE name = ?", (name,))
            if row is None:  # pragma: no cover - defensive
                raise RuntimeError(f"cannot create source {name}")
            return int(row["id"])

    # --------------------------------------------------------------- verification

    async def get_media_file(self, media_file_id: int) -> Row | None:
        return await self._db.fetchone(
            "SELECT id, track_id, path, checksum, verified_at FROM media_files WHERE id = ?",
            (media_file_id,),
        )

    async def register_downloaded_file(
        self,
        track_id: int,
        *,
        path: str,
        format: str,
        size_bytes: int,
        duration_ms: int | None = None,
    ) -> int:
        """Register a downloaded file; verification is a separate step."""

        async with self._db.transaction() as conn:
            existing = await _fetchone(
                conn, "SELECT id FROM media_files WHERE path = ?", (path,)
            )
            if existing is not None:
                return int(existing["id"])
            cursor = await conn.execute(
                """
                INSERT INTO media_files
                    (track_id, role, path, format, size_bytes, duration_ms)
                VALUES (?, 'download', ?, ?, ?, ?)
                """,
                (track_id, path, format, size_bytes, duration_ms),
            )
            media_id = int(cursor.lastrowid or 0)
            await cursor.close()
            return media_id

    async def set_verified(
        self, media_file_id: int, checksum: str, verified_at: str
    ) -> int | None:
        async with self._db.transaction() as conn:
            row = await _fetchone(
                conn,
                "SELECT track_id FROM media_files WHERE id = ?",
                (media_file_id,),
            )
            if row is None:
                return None
            track_id = int(row["track_id"])
            await conn.execute(
                "UPDATE media_files SET checksum = ?, verified_at = ? WHERE id = ?",
                (checksum, verified_at, media_file_id),
            )
            await conn.execute(
                """
                UPDATE tracks
                SET has_local_file = 1, file_missing = 0, last_verified_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (verified_at, verified_at, track_id),
            )
            await self.index_track(track_id, conn)
            return track_id

    async def set_missing(self, media_file_id: int, verified_at: str) -> int | None:
        async with self._db.transaction() as conn:
            row = await _fetchone(
                conn,
                "SELECT track_id FROM media_files WHERE id = ?",
                (media_file_id,),
            )
            if row is None:
                return None
            track_id = int(row["track_id"])
            await conn.execute(
                "UPDATE media_files SET checksum = NULL, verified_at = NULL WHERE id = ?",
                (media_file_id,),
            )
            # One missing file must not mark a track missing when another
            # verified file (e.g. FLAC next to a deleted MP3) still exists.
            verified = await _fetchone(
                conn,
                "SELECT 1 FROM media_files WHERE track_id = ? AND verified_at IS NOT NULL "
                "LIMIT 1",
                (track_id,),
            )
            has_local_file = 1 if verified is not None else 0
            await conn.execute(
                """
                UPDATE tracks
                SET has_local_file = ?, file_missing = ?, last_verified_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    has_local_file,
                    0 if has_local_file else 1,
                    verified_at,
                    verified_at,
                    track_id,
                ),
            )
            return track_id

    async def update_album_cover(self, album_id: int, cover_key: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE albums SET cover_path = ?, updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (cover_key, album_id),
            )

    async def clear_cover_key(self, cover_key: str) -> int:
        """Drop album references to an artwork key evicted from the cache."""

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE albums SET cover_path = NULL, updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE cover_path = ?",
                (cover_key,),
            )
            cleared = int(cursor.rowcount or 0)
            await cursor.close()
            return cleared

    async def album_for_track(self, track_id: int) -> int | None:
        row = await self._db.fetchone(
            "SELECT album_id FROM tracks WHERE id = ? AND album_id IS NOT NULL", (track_id,)
        )
        return int(row["album_id"]) if row is not None else None

    async def album_cover(self, album_id: int) -> str | None:
        row = await self._db.fetchone("SELECT cover_path FROM albums WHERE id = ?", (album_id,))
        return row["cover_path"] if row is not None else None

    async def delete_track(
        self,
        track_id: int,
        *,
        delete_file: bool = False,
        roots: Sequence[Path] = (),
    ) -> tuple[bool, list[str]]:
        """Tombstone a track; optionally unlink its local files.

        The row is kept (``deleted_at``) so history/media references remain
        recoverable. Queue entries are removed, playlist composition is marked
        removed, and the FTS row is dropped. Files stay by default; an explicit
        ``delete_file=true`` unlinks only files inside a configured root that no
        other track references.
        """

        rows = await self._db.fetchall(
            "SELECT path FROM media_files WHERE track_id = ? "
            "AND role IN ('library', 'download')",
            (track_id,),
        )
        paths = [Path(row["path"]) for row in rows if row["path"]]
        resolved_roots = [root.resolve() for root in roots]
        deleted_paths: list[str] = []
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE tracks SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND deleted_at IS NULL",
                (track_id,),
            )
            deleted = cursor.rowcount
            await cursor.close()
            if not deleted:
                return False, []
            await conn.execute("DELETE FROM queue_items WHERE track_id = ?", (track_id,))
            await conn.execute(
                "UPDATE playlist_tracks SET removed_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE track_id = ? AND removed_at IS NULL",
                (track_id,),
            )
            await conn.execute("DELETE FROM tracks_fts WHERE rowid = ?", (track_id,))
            if delete_file:
                for path in paths:
                    try:
                        resolved = path.resolve()
                    except OSError:
                        continue
                    if not any(resolved.is_relative_to(root) for root in resolved_roots):
                        continue
                    shared = await _fetchone(
                        conn,
                        "SELECT 1 FROM media_files WHERE path = ? AND track_id != ? LIMIT 1",
                        (str(path), track_id),
                    )
                    if shared is not None or not resolved.is_file():
                        continue
                    try:
                        await asyncio.to_thread(resolved.unlink)
                        deleted_paths.append(str(resolved))
                    except OSError:
                        continue
        return True, deleted_paths

    async def restore_track(self, track_id: int) -> bool:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE tracks SET deleted_at = NULL, updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND deleted_at IS NOT NULL",
                (track_id,),
            )
            restored = cursor.rowcount
            await cursor.close()
        if restored:
            await self.index_track(track_id)
        return bool(restored)

    async def update_track_metadata(
        self,
        track_id: int,
        *,
        title: str | None = None,
        artists: Sequence[str] | None = None,
        album: str | None = None,
        year: int | None = None,
        genre: str | None = None,
        track_number: int | None = None,
        disc_number: int | None = None,
    ) -> Track | None:
        """Update library metadata (not audio bytes) and refresh FTS.

        Only provided values are changed; an explicit empty artist list is
        rejected by the caller. Album title changes reuse/create an album row.
        """

        current_row = await self._db.fetchone(
            f"SELECT {TRACK_COLUMNS} {TRACK_FROM} "
            f"WHERE t.id = ? AND {TRACK_ALIVE}",
            (track_id,),
        )
        if current_row is None:
            return None
        async with self._db.transaction() as conn:
            updates: list[str] = []
            params: list[Any] = []
            if title is not None:
                updates.extend(["title = ?"])
                params.append(title)
            if genre is not None:
                updates.append("genre = ?")
                params.append(genre)
            if year is not None:
                updates.append("year = ?")
                params.append(year)
            if track_number is not None:
                updates.append("track_number = ?")
                params.append(track_number)
            if disc_number is not None:
                updates.append("disc_number = ?")
                params.append(disc_number)
            artist_names = [
                name.strip() for name in (artists or ()) if name and name.strip()
            ]
            if artists is not None and not artist_names:
                raise ValueError("artists must not be empty")
            if album is not None:
                artist_id = await self._get_or_create_artist(
                    conn, artist_names[0] if artist_names else "Unknown Artist"
                )
                album_id = await self._get_or_create_album(conn, album, artist_id, year)
                updates.append("album_id = ?")
                params.append(album_id)
            if updates:
                updates.append(
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                )
                await conn.execute(
                    f"UPDATE tracks SET {', '.join(updates)} WHERE id = ?",
                    (*params, track_id),
                )
            if artists is not None:
                await conn.execute(
                    "DELETE FROM track_artists WHERE track_id = ?", (track_id,)
                )
                for position, name in enumerate(artist_names):
                    artist_id = await self._get_or_create_artist(conn, name)
                    await conn.execute(
                        "INSERT INTO track_artists (track_id, artist_id, position) "
                        "VALUES (?, ?, ?)",
                        (track_id, artist_id, position),
                    )
        await self.index_track(track_id)
        return await TrackRepository(self._db).get(track_id)

    async def known_media_files(self, roots: list[Path]) -> list[Row]:
        rows = await self._db.fetchall(
            "SELECT id, track_id, path FROM media_files WHERE role IN ('library', 'download')"
        )
        resolved_roots = [root.resolve() for root in roots]
        result = []
        for row in rows:
            path = Path(row["path"])
            try:
                if any(path.resolve().is_relative_to(root) for root in resolved_roots):
                    result.append(row)
            except OSError:
                result.append(row)
        return result


async def _fetchone(
    conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> Row | None:
    cursor = await conn.execute(sql, parameters)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


class QueueRepository:
    """Persistent play queue. Positions are contiguous and unique."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def all(self) -> list[QueueItem]:
        rows = await self._db.fetchall(
            f"SELECT {QUEUE_COLUMNS} {QUEUE_FROM} "
            f"WHERE {TRACK_ALIVE} ORDER BY q.position"
        )
        return [row_to_queue_item(row) for row in rows]

    async def get(self, item_id: int) -> QueueItem | None:
        row = await self._db.fetchone(
            f"SELECT {QUEUE_COLUMNS} {QUEUE_FROM} "
            f"WHERE q.id = ? AND {TRACK_ALIVE}",
            (item_id,),
        )
        return row_to_queue_item(row) if row is not None else None

    async def count(self) -> int:
        return int(
            await self._db.scalar(
                f"SELECT COUNT(*) {QUEUE_FROM} WHERE {TRACK_ALIVE}"
            )
            or 0
        )

    async def find_item_id_by_track(self, track_id: int) -> int | None:
        row = await self._db.fetchone(
            f"SELECT q.id FROM queue_items q JOIN tracks t ON t.id = q.track_id "
            f"WHERE q.track_id = ? AND {TRACK_ALIVE} ORDER BY q.position LIMIT 1",
            (track_id,),
        )
        return int(row["id"]) if row is not None else None

    async def add_many(self, track_ids: Sequence[int]) -> list[int]:
        if not track_ids:
            return []
        async with self._db.transaction() as conn:
            row = await conn.execute("SELECT COALESCE(MAX(position), -1) FROM queue_items")
            result = await row.fetchone()
            await row.close()
            start = int(result[0]) + 1 if result is not None else 0
            created: list[int] = []
            for offset, track_id in enumerate(track_ids):
                cursor = await conn.execute(
                    "INSERT INTO queue_items (track_id, position) VALUES (?, ?)",
                    (track_id, start + offset),
                )
                created.append(int(cursor.lastrowid or 0))
                await cursor.close()
            return created

    async def replace_track(self, item_id: int, track_id: int) -> bool:
        """Point an existing queue item at another track, preserving position."""

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE queue_items SET track_id = ? WHERE id = ?",
                (track_id, item_id),
            )
            changed = cursor.rowcount
            await cursor.close()
            return bool(changed)

    async def remove(self, item_id: int) -> bool:
        async with self._db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM queue_items WHERE id = ?", (item_id,))
            deleted = cursor.rowcount
            await cursor.close()
            if not deleted:
                return False
            await self._renumber(conn)
            return True

    async def clear_except(self, item_id: int | None) -> int:
        """Delete every queue item except ``item_id`` (None clears all)."""

        async with self._db.transaction() as conn:
            if item_id is None:
                cursor = await conn.execute("DELETE FROM queue_items")
            else:
                cursor = await conn.execute(
                    "DELETE FROM queue_items WHERE id <> ?", (item_id,)
                )
            deleted = cursor.rowcount
            await cursor.close()
            await self._renumber(conn)
            return deleted

    async def reorder(self, item_ids: Sequence[int]) -> None:
        async with self._db.transaction() as conn:
            rows = await _fetchall(conn, "SELECT id FROM queue_items ORDER BY position")
            existing = [int(row["id"]) for row in rows]
            if len(item_ids) != len(existing) or set(item_ids) != set(existing):
                raise QueueChangedError(
                    "item_ids must be a permutation of the current queue",
                    details={"expected": existing},
                )
            await conn.execute(
                "UPDATE queue_items SET position = position + ?", (len(existing) + 1,)
            )
            for position, item_id in enumerate(item_ids):
                await conn.execute(
                    "UPDATE queue_items SET position = ? WHERE id = ?", (position, item_id)
                )

    @staticmethod
    async def _renumber(conn: aiosqlite.Connection) -> None:
        rows = await _fetchall(conn, "SELECT id FROM queue_items ORDER BY position")
        for position, row in enumerate(rows):
            await conn.execute(
                "UPDATE queue_items SET position = ? WHERE id = ?", (position, int(row["id"]))
            )


@dataclass(frozen=True)
class SavedPlaybackState:
    track_id: int | None
    queue_item_id: int | None
    position_ms: int
    volume: int
    status: PlayerStatus
    zone_id: str | None
    queue_index: int
    updated_at: str


class PlaybackStateRepository:
    """Singleton ``playback_state`` row (id = 1)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> SavedPlaybackState | None:
        row = await self._db.fetchone("SELECT * FROM playback_state WHERE id = 1")
        if row is None:
            return None
        return SavedPlaybackState(
            track_id=row["track_id"],
            queue_item_id=row["queue_item_id"],
            position_ms=int(row["position_ms"]),
            volume=int(row["volume"]),
            status=row["status"],
            zone_id=row["zone_id"],
            queue_index=int(row["queue_index"]),
            updated_at=row["updated_at"],
        )

    async def save(self, state: SavedPlaybackState) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO playback_state
                    (id, track_id, queue_item_id, position_ms, volume, status,
                     zone_id, queue_index, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    track_id = excluded.track_id,
                    queue_item_id = excluded.queue_item_id,
                    position_ms = excluded.position_ms,
                    volume = excluded.volume,
                    status = excluded.status,
                    zone_id = excluded.zone_id,
                    queue_index = excluded.queue_index,
                    updated_at = excluded.updated_at
                """,
                (
                    state.track_id,
                    state.queue_item_id,
                    state.position_ms,
                    state.volume,
                    state.status,
                    state.zone_id,
                    state.queue_index,
                    state.updated_at,
                ),
            )


class PlayHistoryRepository:
    """Append-only listening history."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, track_id: int, zone_id: str | None) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO play_history (track_id, zone_id) VALUES (?, ?)",
                (track_id, zone_id),
            )

    async def page(self, *, limit: int = 50, offset: int = 0) -> HistoryPage:
        rows = await self._db.fetchall(
            f"""
            SELECT h.id AS history_id, h.played_at, h.zone_id,
                   {TRACK_COLUMNS}
            FROM play_history h
            JOIN tracks t ON t.id = h.track_id
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE {TRACK_ALIVE}
            ORDER BY h.played_at DESC, h.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        total = int(await self._db.scalar("SELECT COUNT(*) FROM play_history") or 0)
        return HistoryPage(
            items=[
                HistoryEntry(
                    id=int(row["history_id"]),
                    track=row_to_track(row),
                    played_at=row["played_at"],
                    zone_id=row["zone_id"],
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def clear(self) -> int:
        async with self._db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM play_history")
            deleted = cursor.rowcount
            await cursor.close()
            return int(deleted or 0)


class SettingsRepository:
    """Key/value settings with JSON-encoded values."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> Any:
        row = await self._db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    async def set(self, key: str, value: Any) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False)),
            )
