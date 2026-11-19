"""Register an existing audio file for P1 real-mode testing.

This is a developer fixture, not the P2 media-library importer: no scanning, no
tag reading, no deduplication. It exists so the real mpv path can be verified
with one local file before the importer is built.

Usage (Linux/Pi, from backend/):
    uv run python -m app.devtools.register_track /path/to/track.flac --title "..."
"""

from __future__ import annotations

import argparse
import asyncio
import wave
from pathlib import Path

from app.config import Settings, ensure_runtime_dirs
from app.database.connection import Database
from app.database.migrations import apply_migrations


def _probe_duration_ms(path: Path) -> int | None:
    """Read WAV duration with the standard library; other formats stay NULL."""

    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as wav:
            frame_rate = wav.getframerate()
            if not frame_rate:
                return None
            return int(round(wav.getnframes() * 1000 / frame_rate))
    except (wave.Error, EOFError, OSError):
        return None


async def register_track(
    path: Path,
    *,
    title: str | None = None,
    artist: str = "Local",
    album: str | None = None,
    year: int | None = None,
) -> int:
    if not path.is_file():
        raise FileNotFoundError(f"audio file not found: {path}")
    resolved = path.resolve()
    track_title = title or resolved.stem
    album_title = album or "Local files"

    settings = Settings()
    ensure_runtime_dirs(settings)
    db = Database(settings.paths.database)
    await db.connect()
    try:
        await apply_migrations(db)
        async with db.transaction() as conn:
            await conn.execute("INSERT OR IGNORE INTO sources (name) VALUES ('local')")
            cursor = await conn.execute("SELECT id FROM sources WHERE name = 'local'")
            source_row = await cursor.fetchone()
            await cursor.close()
            source_id = int(source_row["id"])

            cursor = await conn.execute(
                "SELECT id FROM artists WHERE name = ? ORDER BY id LIMIT 1", (artist,)
            )
            artist_row = await cursor.fetchone()
            await cursor.close()
            if artist_row is None:
                cursor = await conn.execute("INSERT INTO artists (name) VALUES (?)", (artist,))
                artist_id = int(cursor.lastrowid or 0)
                await cursor.close()
            else:
                artist_id = int(artist_row["id"])

            cursor = await conn.execute(
                "SELECT id FROM albums WHERE title = ? AND artist_id = ? ORDER BY id LIMIT 1",
                (album_title, artist_id),
            )
            album_row = await cursor.fetchone()
            await cursor.close()
            if album_row is None:
                cursor = await conn.execute(
                    "INSERT INTO albums (title, artist_id, year) VALUES (?, ?, ?)",
                    (album_title, artist_id, year),
                )
                album_id = int(cursor.lastrowid or 0)
                await cursor.close()
            else:
                album_id = int(album_row["id"])

            cursor = await conn.execute(
                """
                INSERT INTO tracks
                    (title, album_id, year, duration_ms,
                     has_local_file, has_online_source, last_verified_at)
                VALUES (?, ?, ?, ?, 1, 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (track_title, album_id, year, _probe_duration_ms(resolved)),
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
                INSERT INTO media_files (track_id, role, path, format, size_bytes, verified_at)
                VALUES (?, 'library', ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    track_id,
                    str(resolved),
                    resolved.suffix.lstrip(".").lower(),
                    resolved.stat().st_size,
                ),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO track_sources "
                "(track_id, source_id, external_id, availability) "
                "VALUES (?, ?, ?, 'available')",
                (track_id, source_id, f"local:{resolved}"),
            )
        return track_id
    finally:
        await db.close()


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="path to an audio file")
    parser.add_argument("--title", default=None)
    parser.add_argument("--artist", default="Local")
    parser.add_argument("--album", default=None)
    parser.add_argument("--year", type=int, default=None)
    args = parser.parse_args()
    track_id = await register_track(
        args.path,
        title=args.title,
        artist=args.artist,
        album=args.album,
        year=args.year,
    )
    print(f"registered track id={track_id} path={args.path.resolve()}")


if __name__ == "__main__":
    asyncio.run(_main())
