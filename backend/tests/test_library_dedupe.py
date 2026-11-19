"""Dedupe rules and idempotence at the repository level."""

from __future__ import annotations

from pathlib import Path

from app.core.models import LocalMediaMetadata
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.repositories import LibraryRepository


def _metadata(
    path: Path,
    *,
    relative: str,
    title: str = "Same Title",
    artist: str = "Same Artist",
    album: str | None = "Same Album",
    duration_ms: int = 3000,
    size_bytes: int = 1234,
    bitrate: int | None = None,
    sample_rate: int | None = None,
    channels: int | None = None,
) -> LocalMediaMetadata:
    return LocalMediaMetadata(
        path=path,
        relative_path=relative,
        format="mp3",
        title=title,
        artists=[artist],
        album=album,
        albumartist=artist,
        year=2024,
        genre="Test",
        track_number=1,
        disc_number=1,
        label="Label",
        duration_ms=duration_ms,
        size_bytes=size_bytes,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
    )


async def test_dedupe_rules(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        await apply_migrations(database)
        library = LibraryRepository(database)
        source_id = await library.get_or_create_source("local")

        first = await library.import_local(
            _metadata(tmp_path / "a.mp3", relative="a.mp3"), source_id
        )
        assert first.status == "created"

        # Same metadata through a different path/container: same track, second file.
        second = await library.import_local(
            _metadata(tmp_path / "b.flac", relative="b.flac", duration_ms=3100),
            source_id,
        )
        assert second.track_id == first.track_id
        assert second.status == "updated"

        # Different album -> new track.
        different_album = await library.import_local(
            _metadata(tmp_path / "c.mp3", relative="c.mp3", album="Other Album"),
            source_id,
        )
        assert different_album.track_id != first.track_id
        assert different_album.status == "created"

        # Same title/album but duration far outside tolerance -> new track.
        different_duration = await library.import_local(
            _metadata(tmp_path / "d.mp3", relative="d.mp3", duration_ms=9000),
            source_id,
        )
        assert different_duration.track_id not in {
            first.track_id,
            different_album.track_id,
        }

        # Same title/album/duration but different artist -> new track.
        different_artist = await library.import_local(
            _metadata(tmp_path / "e.mp3", relative="e.mp3", artist="Other Artist"),
            source_id,
        )
        assert different_artist.track_id not in {
            first.track_id,
            different_album.track_id,
            different_duration.track_id,
        }

        # Re-import by the same external id is a no-op.
        again = await library.import_local(
            _metadata(tmp_path / "a.mp3", relative="a.mp3"), source_id
        )
        assert again.track_id == first.track_id
        assert again.status == "skipped"

        tracks = await database.scalar("SELECT COUNT(*) FROM tracks")
        media = await database.scalar("SELECT COUNT(*) FROM media_files")
        assert tracks == 4
        assert media == 5
    finally:
        await database.close()


async def test_external_id_is_scoped_by_source(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        await apply_migrations(database)
        library = LibraryRepository(database)
        local = await library.get_or_create_source("local")
        other = await library.get_or_create_source("other")
        first = await library.import_local(
            _metadata(tmp_path / "a.mp3", relative="a.mp3"), local
        )
        # Same external id string in another source does not collide; metadata
        # matching is allowed to merge it into the existing track (SPEC §6).
        second = await library.import_local(
            _metadata(tmp_path / "b.mp3", relative="a.mp3"), other
        )
        assert second.track_id == first.track_id
        assert second.status == "updated"
    finally:
        await database.close()


async def test_reimport_updates_metadata_and_resets_checksum(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        await apply_migrations(database)
        library = LibraryRepository(database)
        source = await library.get_or_create_source("local")
        first = await library.import_local(
            _metadata(tmp_path / "a.mp3", relative="a.mp3", title="Old Title"), source
        )
        assert first.status == "created"
        await library.set_verified(
            first.media_file_id, "old-checksum", "2024-01-01T00:00:00.000Z"
        )

        changed = await library.import_local(
            _metadata(
                tmp_path / "a.mp3",
                relative="a.mp3",
                title="New Title",
                artist="New Artist",
                album="New Album",
                duration_ms=4321,
                size_bytes=9999,
                bitrate=320000,
                sample_rate=48000,
                channels=2,
            ),
            source,
        )
        assert changed.track_id == first.track_id
        assert changed.status == "updated"

        track = await database.fetchone(
            "SELECT title, duration_ms, genre, label, year, track_number, disc_number, "
            "album_id FROM tracks WHERE id = ?",
            (first.track_id,),
        )
        assert track is not None
        assert track["title"] == "New Title"
        assert track["duration_ms"] == 4321
        assert track["year"] == 2024
        assert track["track_number"] == 1
        assert track["disc_number"] == 1
        album_title = await database.scalar(
            "SELECT title FROM albums WHERE id = ?", (track["album_id"],)
        )
        assert album_title == "New Album"
        assert await database.scalar(
            "SELECT title FROM tracks_fts WHERE rowid = ?", (first.track_id,)
        ) == "New Title"
        assert await database.scalar(
            "SELECT artists FROM tracks_fts WHERE rowid = ?", (first.track_id,)
        ) == "New Artist"
        artists = await database.fetchall(
            "SELECT a.name FROM track_artists ta JOIN artists a ON a.id = ta.artist_id "
            "WHERE ta.track_id = ? ORDER BY ta.position",
            (first.track_id,),
        )
        assert [row["name"] for row in artists] == ["New Artist"]

        media = await database.fetchone(
            "SELECT size_bytes, duration_ms, bitrate, sample_rate, channels, checksum, "
            "verified_at FROM media_files WHERE id = ?",
            (first.media_file_id,),
        )
        assert media is not None
        assert media["size_bytes"] == 9999
        assert media["duration_ms"] == 4321
        assert media["bitrate"] == 320000
        assert media["sample_rate"] == 48000
        assert media["channels"] == 2
        assert media["checksum"] is None
        assert media["verified_at"] is None

        again = await library.import_local(
            _metadata(
                tmp_path / "a.mp3",
                relative="a.mp3",
                title="New Title",
                artist="New Artist",
                album="New Album",
                duration_ms=4321,
                size_bytes=9999,
                bitrate=320000,
                sample_rate=48000,
                channels=2,
            ),
            source,
        )
        assert again.status == "skipped"
    finally:
        await database.close()


async def test_dedupe_without_album_tag(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        await apply_migrations(database)
        library = LibraryRepository(database)
        source = await library.get_or_create_source("local")
        first = await library.import_local(
            _metadata(tmp_path / "a.mp3", relative="a.mp3", album=None), source
        )
        second = await library.import_local(
            _metadata(
                tmp_path / "b.mp3", relative="b.mp3", album=None, duration_ms=3100
            ),
            source,
        )
        assert second.track_id == first.track_id
        assert second.status == "updated"
        assert await database.scalar("SELECT COUNT(*) FROM tracks") == 1
        assert await database.scalar(
            "SELECT title FROM albums WHERE id = ?", (first.album_id,)
        ) == "Unknown Album"
    finally:
        await database.close()
