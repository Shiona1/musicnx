"""SQLite normalization layer for the Yandex Music catalog (P3).

Only SQL lives here: the service layer never builds queries, and this module
never calls the network. Rows are merged by ``track_sources(source_id,
external_id)`` so repeated catalog/wave calls are idempotent and keep a stable
internal ``track_id`` for the queue and player (docs/API.md §6.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import aiosqlite

from app.core.models import Track
from app.database.connection import Database, Row
from app.database.repositories import (
    TRACK_COLUMNS,
    TRACK_FROM,
    TrackRepository,
    row_to_track,
)
from app.integrations.yandex_music.mapper import (
    NormalizedAlbum,
    NormalizedArtist,
    NormalizedPlaylist,
    NormalizedTrack,
)

SOURCE_NAME = "yandex"
UNKNOWN_ARTIST = "Unknown Artist"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _fetchone(
    conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> Row | None:
    cursor = await conn.execute(sql, parameters)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(
    conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> list[Row]:
    cursor = await conn.execute(sql, parameters)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


def _now_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def with_provenance(
    track: Track,
    external_id: str | None,
    *,
    preview: bool = False,
    liked: bool | None = None,
) -> Track:
    """Attach P3 catalog provenance to a DTO built from shared SQL columns."""

    update: dict[str, Any] = {"source": "yandex", "preview": preview}
    if external_id is not None:
        update["external_id"] = external_id
    if liked is not None:
        update["liked"] = liked
    return track.model_copy(update=update)


class YandexRepository:
    """Upsert/read the Yandex slice of the unified SQLite catalog."""

    def __init__(self, db: Database, tracks: TrackRepository) -> None:
        self._db = db
        self._tracks = tracks
        self._source_id: int | None = None

    # ----------------------------------------------------------------- source

    async def source_id(self) -> int:
        if self._source_id is None:
            async with self._db.transaction() as conn:
                self._source_id = await self._ensure_source(conn)
        return self._source_id

    @staticmethod
    async def _ensure_source(conn: aiosqlite.Connection) -> int:
        await conn.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (SOURCE_NAME,))
        row = await _fetchone(conn, "SELECT id FROM sources WHERE name = ?", (SOURCE_NAME,))
        if row is None:  # pragma: no cover - defensive
            raise RuntimeError(f"cannot create source {SOURCE_NAME}")
        return int(row["id"])

    # --------------------------------------------------------------- artists

    async def upsert_artist(self, artist: NormalizedArtist) -> int:
        source_id = await self.source_id()
        async with self._db.transaction() as conn:
            return await self._upsert_artist(conn, source_id, artist)

    async def artist_external_id(self, artist_id: int) -> str | None:
        """Resolve a local artist row back to its Yandex id."""

        source_id = await self.source_id()
        row = await self._db.fetchone(
            "SELECT external_id FROM artist_sources "
            "WHERE artist_id = ? AND source_id = ?",
            (artist_id, source_id),
        )
        return str(row["external_id"]) if row is not None else None

    async def album_external_id(self, album_id: int) -> str | None:
        """Resolve a local album row back to its Yandex id."""

        source_id = await self.source_id()
        row = await self._db.fetchone(
            "SELECT external_id FROM album_sources "
            "WHERE album_id = ? AND source_id = ?",
            (album_id, source_id),
        )
        return str(row["external_id"]) if row is not None else None

    async def upsert_artists(self, artists: Sequence[NormalizedArtist]) -> list[int]:
        if not artists:
            return []
        source_id = await self.source_id()
        async with self._db.transaction() as conn:
            return [await self._upsert_artist(conn, source_id, item) for item in artists]

    async def _upsert_artist(
        self, conn: aiosqlite.Connection, source_id: int, artist: NormalizedArtist
    ) -> int:
        mapped = await _fetchone(
            conn,
            "SELECT artist_id FROM artist_sources WHERE source_id = ? AND external_id = ?",
            (source_id, artist.external_id),
        )
        if mapped is not None:
            artist_id = int(mapped["artist_id"])
            await conn.execute(
                f"UPDATE artists SET name = ?, updated_at = {_now_sql()} "
                "WHERE id = ? AND name <> ?",
                (artist.name, artist_id, artist.name),
            )
            return artist_id

        # Reuse an identically named local artist unless it is already bound to
        # another Yandex id (then it is a different artist that shares a name).
        if artist.name != UNKNOWN_ARTIST:
            existing = await _fetchone(
                conn,
                "SELECT id FROM artists WHERE name = ? ORDER BY id LIMIT 1",
                (artist.name,),
            )
            if existing is not None:
                candidate = int(existing["id"])
                bound = await _fetchone(
                    conn,
                    "SELECT 1 FROM artist_sources WHERE artist_id = ? AND source_id = ?",
                    (candidate, source_id),
                )
                if bound is None:
                    await conn.execute(
                        "INSERT OR IGNORE INTO artist_sources "
                        "(artist_id, source_id, external_id) VALUES (?, ?, ?)",
                        (candidate, source_id, artist.external_id),
                    )
                    return candidate

        cursor = await conn.execute("INSERT INTO artists (name) VALUES (?)", (artist.name,))
        artist_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await conn.execute(
            "INSERT OR IGNORE INTO artist_sources (artist_id, source_id, external_id) "
            "VALUES (?, ?, ?)",
            (artist_id, source_id, artist.external_id),
        )
        return artist_id

    async def _artist_id_by_name(
        self, conn: aiosqlite.Connection, name: str
    ) -> int:
        row = await _fetchone(
            conn, "SELECT id FROM artists WHERE name = ? ORDER BY id LIMIT 1", (name,)
        )
        if row is not None:
            return int(row["id"])
        cursor = await conn.execute("INSERT INTO artists (name) VALUES (?)", (name,))
        artist_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return artist_id

    async def get_artist_id(self, external_id: str) -> int | None:
        row = await self._db.fetchone(
            """
            SELECT a.id FROM artists a
            JOIN artist_sources s ON s.artist_id = a.id
            WHERE s.source_id = ? AND s.external_id = ?
            """,
            (await self.source_id(), external_id),
        )
        return int(row["id"]) if row is not None else None

    async def artist_name(self, external_id: str) -> str | None:
        row = await self._db.fetchone(
            """
            SELECT a.name FROM artists a
            JOIN artist_sources s ON s.artist_id = a.id
            WHERE s.source_id = ? AND s.external_id = ?
            """,
            (await self.source_id(), external_id),
        )
        return row["name"] if row is not None else None

    # ---------------------------------------------------------------- albums

    async def upsert_album(
        self, album: NormalizedAlbum, *, artist_id: int | None = None
    ) -> int:
        source_id = await self.source_id()
        async with self._db.transaction() as conn:
            if artist_id is None and album.artists:
                artist_id = await self._upsert_artist(conn, source_id, album.artists[0])
            return await self._upsert_album(conn, source_id, album, artist_id)

    async def upsert_albums(self, albums: Sequence[NormalizedAlbum]) -> list[int]:
        if not albums:
            return []
        source_id = await self.source_id()
        ids: list[int] = []
        async with self._db.transaction() as conn:
            for album in albums:
                artist_id = None
                if album.artists:
                    artist_id = await self._upsert_artist(conn, source_id, album.artists[0])
                ids.append(await self._upsert_album(conn, source_id, album, artist_id))
        return ids

    async def _upsert_album(
        self,
        conn: aiosqlite.Connection,
        source_id: int,
        album: NormalizedAlbum,
        artist_id: int | None,
    ) -> int:
        mapped = await _fetchone(
            conn,
            "SELECT album_id FROM album_sources WHERE source_id = ? AND external_id = ?",
            (source_id, album.external_id),
        )
        if mapped is not None:
            album_id = int(mapped["album_id"])
            await conn.execute(
                f"""
                UPDATE albums
                SET title = ?, year = COALESCE(?, year),
                    artist_id = COALESCE(?, artist_id),
                    cover_path = COALESCE(?, cover_path), updated_at = {_now_sql()}
                WHERE id = ?
                """,
                (album.title, album.year, artist_id, album.cover_url, album_id),
            )
            return album_id

        cursor = await conn.execute(
            "INSERT INTO albums (title, artist_id, year, cover_path) VALUES (?, ?, ?, ?)",
            (album.title, artist_id, album.year, album.cover_url),
        )
        album_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await conn.execute(
            "INSERT OR IGNORE INTO album_sources (album_id, source_id, external_id) "
            "VALUES (?, ?, ?)",
            (album_id, source_id, album.external_id),
        )
        return album_id

    async def album_id_by_external(self, external_id: str) -> int | None:
        row = await self._db.fetchone(
            "SELECT album_id FROM album_sources WHERE source_id = ? AND external_id = ?",
            (await self.source_id(), external_id),
        )
        return int(row["album_id"]) if row is not None else None

    # ---------------------------------------------------------------- tracks

    async def upsert_track(self, track: NormalizedTrack) -> int:
        ids = await self.upsert_tracks([track])
        return ids[0]

    async def upsert_tracks(self, tracks: Sequence[NormalizedTrack]) -> list[int]:
        if not tracks:
            return []
        source_id = await self.source_id()
        async with self._db.transaction() as conn:
            return [await self._upsert_track(conn, source_id, track) for track in tracks]

    async def _upsert_track(
        self, conn: aiosqlite.Connection, source_id: int, track: NormalizedTrack
    ) -> int:
        album_id: int | None = None
        if track.album is not None:
            album_artist_id = None
            if track.album.artists:
                album_artist_id = await self._upsert_artist(
                    conn, source_id, track.album.artists[0]
                )
            elif track.artists:
                album_artist_id = await self._upsert_artist(
                    conn, source_id, track.artists[0]
                )
            album_id = await self._upsert_album(
                conn, source_id, track.album, album_artist_id
            )
            # The track DTO carries the canonical cover even when the album
            # payload omitted it; persist it so player/library rows keep art.
            if track.cover_url:
                await conn.execute(
                    "UPDATE albums SET cover_path = COALESCE(cover_path, ?) WHERE id = ?",
                    (track.cover_url, album_id),
                )

        artist_ids = [
            await self._upsert_artist(conn, source_id, artist)
            for artist in track.artists
        ]
        if not artist_ids:
            artist_ids = [await self._artist_id_by_name(conn, UNKNOWN_ARTIST)]

        track_id, external_changed = await self._resolve_track(
            conn, source_id, track
        )
        availability = "available" if track.available else "unavailable"
        if track_id is None:
            cursor = await conn.execute(
                """
                INSERT INTO tracks
                    (title, album_id, duration_ms, year, has_local_file,
                     has_online_source, file_missing, source_unavailable)
                VALUES (?, ?, ?, ?, 0, 1, 0, ?)
                """,
                (
                    track.title,
                    album_id,
                    track.duration_ms,
                    track.year,
                    0 if track.available else 1,
                ),
            )
            track_id = int(cursor.lastrowid or 0)
            await cursor.close()
            await conn.execute(
                """
                INSERT INTO track_sources
                    (track_id, source_id, external_id, availability, last_checked_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (track_id, source_id, track.external_id, availability),
            )
        else:
            await conn.execute(
                f"""
                UPDATE tracks
                SET title = ?, album_id = ?, duration_ms = ?, year = COALESCE(?, year),
                    has_online_source = 1, file_missing = 0, source_unavailable = ?,
                    updated_at = {_now_sql()}
                WHERE id = ?
                """,
                (
                    track.title,
                    album_id,
                    track.duration_ms,
                    track.year,
                    0 if track.available else 1,
                    track_id,
                ),
            )
            if external_changed:
                await conn.execute(
                    """
                    UPDATE track_sources
                    SET external_id = ?, availability = ?,
                        last_checked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE track_id = ? AND source_id = ?
                    """,
                    (track.external_id, availability, track_id, source_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE track_sources
                    SET availability = ?,
                        last_checked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE track_id = ? AND source_id = ?
                    """,
                    (availability, track_id, source_id),
                )

        await conn.execute("DELETE FROM track_artists WHERE track_id = ?", (track_id,))
        for position, artist_id in enumerate(artist_ids):
            await conn.execute(
                "INSERT OR IGNORE INTO track_artists (track_id, artist_id, position) "
                "VALUES (?, ?, ?)",
                (track_id, artist_id, position),
            )
        await self._index_track(conn, track_id, track)
        return track_id

    async def _resolve_track(
        self,
        conn: aiosqlite.Connection,
        source_id: int,
        track: NormalizedTrack,
    ) -> tuple[int | None, bool]:
        """Return ``(track_id, external_id_changed)`` for a normalized track.

        The album segment of ``external_id`` can change when Yandex re-uploads a
        track into another album; ``UNIQUE(track_id, source_id)`` allows a single
        Yandex row per track, so the row is migrated instead of duplicated.
        """

        exact = await _fetchone(
            conn,
            "SELECT track_id FROM track_sources WHERE source_id = ? AND external_id = ?",
            (source_id, track.external_id),
        )
        if exact is not None:
            return int(exact["track_id"]), False

        prefix = await _fetchone(
            conn,
            "SELECT track_id FROM track_sources WHERE source_id = ? "
            "AND external_id LIKE ? ESCAPE '\\' ORDER BY id LIMIT 1",
            (source_id, f"{_escape_like(track.yandex_id)}:%"),
        )
        if prefix is not None:
            return int(prefix["track_id"]), True
        return None, False

    @staticmethod
    async def _index_track(
        conn: aiosqlite.Connection, track_id: int, track: NormalizedTrack
    ) -> None:
        """Keep the shared FTS5 index in sync (shared by library search)."""

        artists = " ".join(artist.name for artist in track.artists)
        album_title = track.album.title if track.album is not None else ""
        await conn.execute("DELETE FROM tracks_fts WHERE rowid = ?", (track_id,))
        await conn.execute(
            "INSERT INTO tracks_fts (rowid, title, artists, album) VALUES (?, ?, ?, ?)",
            (track_id, track.title, artists, album_title),
        )

    # ----------------------------------------------------------------- reads

    async def get_external_id(self, track_id: int) -> str | None:
        row = await self._db.fetchone(
            "SELECT external_id FROM track_sources WHERE track_id = ? AND source_id = ?",
            (track_id, await self.source_id()),
        )
        return row["external_id"] if row is not None else None

    async def get_track_by_external(
        self,
        external_id: str,
        *,
        preview: bool = False,
        liked: bool | None = None,
    ) -> Track | None:
        row = await self._db.fetchone(
            f"""
            SELECT {TRACK_COLUMNS}, ts.external_id AS yandex_external_id
            {TRACK_FROM}
            JOIN track_sources ts ON ts.track_id = t.id AND ts.source_id = ?
            WHERE ts.external_id = ?
            """,
            (await self.source_id(), external_id),
        )
        if row is None:
            return None
        return with_provenance(
            row_to_track(row), row["yandex_external_id"], preview=preview, liked=liked
        )

    async def list_tracks(
        self,
        track_ids: Sequence[int],
        *,
        preview: bool = False,
        liked: bool | None = None,
    ) -> list[Track]:
        """Fetch tracks in the requested order, with Yandex provenance attached."""

        if not track_ids:
            return []
        source_id = await self.source_id()
        placeholders = ", ".join("?" for _ in track_ids)
        rows = await self._db.fetchall(
            f"""
            SELECT {TRACK_COLUMNS}, ts.external_id AS yandex_external_id
            {TRACK_FROM}
            LEFT JOIN track_sources ts ON ts.track_id = t.id AND ts.source_id = ?
            WHERE t.id IN ({placeholders})
            """,
            (source_id, *track_ids),
        )
        by_id = {
            int(row["track_id"]): with_provenance(
                row_to_track(row), row["yandex_external_id"], preview=preview, liked=liked
            )
            for row in rows
        }
        return [by_id[track_id] for track_id in track_ids if track_id in by_id]

    # ------------------------------------------------------------- playlists

    async def upsert_playlist(self, playlist: NormalizedPlaylist) -> int:
        source_id = await self.source_id()
        async with self._db.transaction() as conn:
            return await self._upsert_playlist_row(
                conn, source_id, playlist.external_id, playlist.title, "user",
                playlist.revision, playlist.track_count,
            )

    async def upsert_system_playlist(
        self, *, key: str, title: str, kind: str, track_count: int
    ) -> int:
        """Likes/wave are single rows keyed by a synthetic external id."""

        source_id = await self.source_id()
        async with self._db.transaction() as conn:
            return await self._upsert_playlist_row(
                conn, source_id, key, title, kind, None, track_count
            )

    async def _upsert_playlist_row(
        self,
        conn: aiosqlite.Connection,
        source_id: int,
        external_id: str,
        title: str,
        kind: str,
        revision: int | None,
        track_count: int,
    ) -> int:
        row = await _fetchone(
            conn,
            "SELECT id FROM playlists WHERE source_id = ? AND external_id = ?",
            (source_id, external_id),
        )
        if row is not None:
            playlist_id = int(row["id"])
            await conn.execute(
                f"""
                UPDATE playlists
                SET title = ?, revision = COALESCE(?, revision), track_count = ?,
                    synced_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_at = {_now_sql()}
                WHERE id = ?
                """,
                (title, str(revision) if revision is not None else None, track_count, playlist_id),
            )
            return playlist_id
        cursor = await conn.execute(
            """
            INSERT INTO playlists
                (title, source_id, external_id, revision, kind, track_count, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (
                title,
                source_id,
                external_id,
                str(revision) if revision is not None else None,
                kind,
                track_count,
            ),
        )
        playlist_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return playlist_id

    async def upsert_playlist_tracks(
        self, playlist_id: int, positions: Sequence[tuple[int, int]]
    ) -> None:
        """Merge ``(position, track_id)`` pairs, so paged fetches are additive."""

        if not positions:
            return
        async with self._db.transaction() as conn:
            await conn.executemany(
                """
                INSERT INTO playlist_tracks (playlist_id, track_id, position)
                VALUES (?, ?, ?)
                ON CONFLICT (playlist_id, position) DO UPDATE SET
                    track_id = excluded.track_id, removed_at = NULL
                """,
                [(playlist_id, track_id, position) for position, track_id in positions],
            )

    async def replace_playlist_tracks(
        self, playlist_id: int, track_ids: Sequence[int]
    ) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
            )
            await conn.executemany(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) "
                "VALUES (?, ?, ?)",
                [(playlist_id, track_id, position) for position, track_id in enumerate(track_ids)],
            )

    async def playlist_track_ids(self, playlist_id: int) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT track_id FROM playlist_tracks WHERE playlist_id = ? "
            "AND removed_at IS NULL ORDER BY position",
            (playlist_id,),
        )
        return [int(row["track_id"]) for row in rows]
