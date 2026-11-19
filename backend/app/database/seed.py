"""Idempotent seeding of the mock catalog (mock mode only)."""

from __future__ import annotations

from app.database.connection import Database
from app.integrations.yandex_music.mock import MockYandexAdapter

MOCK_PLAYLIST_EXTERNAL_ID = "mock:playlist:1"
MOCK_PLAYLIST_TITLE = "Mock Radio"


async def _artist_id(conn, name: str) -> int:
    cursor = await conn.execute(
        "SELECT id FROM artists WHERE name = ? ORDER BY id LIMIT 1", (name,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is not None:
        return int(row["id"])
    cursor = await conn.execute("INSERT INTO artists (name) VALUES (?)", (name,))
    artist_id = int(cursor.lastrowid or 0)
    await cursor.close()
    return artist_id


async def _album_id(conn, title: str, artist_id: int, year: int) -> int:
    cursor = await conn.execute(
        "SELECT id FROM albums WHERE title = ? AND artist_id = ? ORDER BY id LIMIT 1",
        (title, artist_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is not None:
        return int(row["id"])
    cursor = await conn.execute(
        "INSERT INTO albums (title, artist_id, year) VALUES (?, ?, ?)",
        (title, artist_id, year),
    )
    album_id = int(cursor.lastrowid or 0)
    await cursor.close()
    return album_id


async def _existing_track_id(conn, source_id: int, external_id: str) -> int | None:
    cursor = await conn.execute(
        "SELECT track_id FROM track_sources WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row["track_id"]) if row is not None else None


async def seed_mock_catalog(db: Database, adapter: MockYandexAdapter | None = None) -> int:
    """Insert or reuse mock tracks; return the number of tracks in the playlist."""

    adapter = adapter or MockYandexAdapter()
    catalog = adapter.catalog()

    async with db.transaction() as conn:
        await conn.execute("INSERT OR IGNORE INTO sources (name) VALUES ('mock')")
        cursor = await conn.execute("SELECT id FROM sources WHERE name = 'mock'")
        source_row = await cursor.fetchone()
        await cursor.close()
        if source_row is None:  # pragma: no cover - defensive
            raise RuntimeError("failed to create mock source")
        source_id = int(source_row["id"])

        await conn.execute(
            """
            INSERT OR IGNORE INTO playlists
                (title, source_id, external_id, kind, track_count)
            VALUES (?, ?, ?, 'local', 0)
            """,
            (MOCK_PLAYLIST_TITLE, source_id, MOCK_PLAYLIST_EXTERNAL_ID),
        )
        cursor = await conn.execute(
            "SELECT id FROM playlists WHERE source_id = ? AND external_id = ?",
            (source_id, MOCK_PLAYLIST_EXTERNAL_ID),
        )
        playlist_row = await cursor.fetchone()
        await cursor.close()
        if playlist_row is None:  # pragma: no cover - defensive
            raise RuntimeError("failed to create mock playlist")
        playlist_id = int(playlist_row["id"])

        track_ids: list[int] = []
        for entry in catalog:
            existing_id = await _existing_track_id(conn, source_id, entry.external_id)
            if existing_id is not None:
                track_ids.append(existing_id)
                continue

            artist_id = await _artist_id(conn, entry.artist)
            album_id = await _album_id(conn, entry.album, artist_id, entry.year)
            cursor = await conn.execute(
                """
                INSERT INTO tracks
                    (title, album_id, duration_ms, genre, year, track_number,
                     disc_number, has_local_file, has_online_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)
                """,
                (
                    entry.title,
                    album_id,
                    entry.duration_ms,
                    entry.genre,
                    entry.year,
                    entry.track_number,
                    entry.disc_number,
                ),
            )
            track_id = int(cursor.lastrowid or 0)
            await cursor.close()
            await conn.execute(
                "INSERT OR IGNORE INTO track_artists (track_id, artist_id, position) "
                "VALUES (?, ?, 0)",
                (track_id, artist_id),
            )
            await conn.execute(
                """
                INSERT INTO track_sources (track_id, source_id, external_id, availability)
                VALUES (?, ?, ?, 'available')
                """,
                (track_id, source_id, entry.external_id),
            )
            track_ids.append(track_id)

        cursor = await conn.execute(
            "SELECT track_id, position FROM playlist_tracks "
            "WHERE playlist_id = ? AND removed_at IS NULL",
            (playlist_id,),
        )
        existing_positions = {
            int(row["track_id"]): int(row["position"]) for row in await cursor.fetchall()
        }
        await cursor.close()

        next_position = max(existing_positions.values(), default=-1) + 1
        for track_id in track_ids:
            if track_id in existing_positions:
                continue
            await conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, track_id, next_position),
            )
            existing_positions[track_id] = next_position
            next_position += 1

        await conn.execute(
            """
            UPDATE playlists
            SET track_count = ?, synced_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (len(existing_positions), playlist_id),
        )
        return len(existing_positions)
