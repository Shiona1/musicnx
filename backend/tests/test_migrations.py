"""SQLite setup and migrations."""

from __future__ import annotations

from pathlib import Path

from app.database.connection import Database
from app.database.migrations import _statements, apply_migrations


async def test_migrations_apply_once_and_are_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "db" / "musicnx.db")
    await database.connect()
    try:
        version = await apply_migrations(database)
        assert version >= 4
        first = await database.fetchall("SELECT version FROM schema_migrations ORDER BY version")
        assert [row["version"] for row in first] == [1, 2, 3, 4]

        version_again = await apply_migrations(database)
        assert version_again == version
        second = await database.fetchall("SELECT COUNT(*) AS c FROM schema_migrations")
        assert second[0]["c"] == 4
    finally:
        await database.close()


async def test_pragmas_and_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        journal = await database.fetchone("PRAGMA journal_mode")
        foreign_keys = await database.fetchone("PRAGMA foreign_keys")
        synchronous = await database.fetchone("PRAGMA synchronous")
        assert journal is not None and str(journal[0]).lower() == "wal"
        assert foreign_keys is not None and int(foreign_keys[0]) == 1
        assert synchronous is not None and int(synchronous[0]) == 1  # NORMAL
    finally:
        await database.close()


async def test_core_tables_exist(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        await apply_migrations(database)
        rows = await database.fetchall(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
        names = {row["name"] for row in rows}
        expected = {
            "sources",
            "artists",
            "artist_sources",
            "albums",
            "album_sources",
            "tracks",
            "track_sources",
            "track_artists",
            "media_files",
            "playlists",
            "playlist_tracks",
            "lyrics",
            "play_history",
            "playback_state",
            "queue_items",
            "download_tasks",
            "audio_zones",
            "settings",
            "sync_runs",
            "schema_migrations",
            "tracks_fts",
        }
        assert expected <= names
    finally:
        await database.close()


async def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = Database(tmp_path / "musicnx.db")
    await database.connect()
    try:
        await apply_migrations(database)
        try:
            async with database.transaction() as conn:
                await conn.execute(
                    "INSERT INTO queue_items (track_id, position) VALUES (9999, 0)"
                )
        except Exception as exc:
            assert "FOREIGN KEY" in str(exc).upper()
        else:  # pragma: no cover - would be a bug
            raise AssertionError("foreign key was not enforced")
    finally:
        await database.close()


def test_statement_splitter_handles_comments_and_quotes() -> None:
    script = """
    -- comment; with semicolon
    CREATE TABLE a (x TEXT DEFAULT 'a;b');
    /* block; comment */ INSERT INTO a (x) VALUES ('it''s');
    """
    statements = _statements(script)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert "it''s" in statements[1]
