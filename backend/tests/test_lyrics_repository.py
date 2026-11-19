"""Repository tests for the ``lyrics`` cache on a temporary SQLite database.

Every test builds its own temp DB through ``Database`` + ``apply_migrations``;
there is no network, no Yandex token and no Raspberry Pi involved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.errors import NotFoundError
from app.lyrics.repository import (
    LYRICS_FORMATS,
    OFFSET_LIMIT_MS,
    LyricsRepository,
    clamp_offset,
    normalize_language,
)

LRC_RU = "[ar:Оркестр]\n[00:01.00]строка\n[00:02.50]вторая"
PLAIN_RU = "строка\nвторая"


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "musicnx.db")
    await db.connect()
    await apply_migrations(db)
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def repo(database: Database) -> LyricsRepository:
    return LyricsRepository(database)


async def create_track(database: Database, title: str = "Track") -> int:
    async with database.transaction() as conn:
        cursor = await conn.execute("INSERT INTO tracks (title) VALUES (?)", (title,))
        track_id = int(cursor.lastrowid)
        await cursor.close()
    return track_id


async def row_count(database: Database, track_id: int) -> int:
    count = await database.scalar("SELECT COUNT(*) FROM lyrics WHERE track_id = ?", (track_id,))
    return int(count or 0)


async def test_save_and_get_roundtrip(repo: LyricsRepository, database: Database) -> None:
    track_id = await create_track(database)

    record = await repo.save(
        track_id,
        language=" RU ",
        format="lrc",
        content=LRC_RU,
        source="yandex",
    )

    assert record.track_id == track_id
    assert record.language == "ru"
    assert record.format == "lrc"
    assert record.content == LRC_RU
    assert record.source == "yandex"
    assert record.offset_ms == 0
    assert record.updated_at

    loaded = await repo.get(track_id)
    assert loaded == record
    assert loaded is not None and loaded.content == LRC_RU


async def test_get_returns_none_for_unknown_track(repo: LyricsRepository) -> None:
    assert await repo.get(999_999) is None
    assert await repo.get(999_999, "ru") is None


async def test_save_upserts_and_preserves_user_offset(
    repo: LyricsRepository, database: Database
) -> None:
    track_id = await create_track(database)
    first = await repo.save(track_id, language="ru", format="plain", content="v1")
    assert await repo.set_offset(track_id, 2_500) is True

    # Backdate the row so the refreshed ``updated_at`` is deterministic.
    async with database.transaction() as conn:
        await conn.execute(
            "UPDATE lyrics SET updated_at = '2000-01-01T00:00:00.000Z' WHERE track_id = ?",
            (track_id,),
        )

    second = await repo.save(
        track_id,
        language="ru",
        format="plain",
        content="v2",
        source="user",
    )

    assert await row_count(database, track_id) == 1
    assert second.content == "v2"
    assert second.source == "user"
    assert second.offset_ms == 2_500  # upsert must not reset the user offset
    assert second.updated_at > "2000-01-01T00:00:00.000Z"
    assert second.updated_at >= first.updated_at

    loaded = await repo.get(track_id, "ru")
    assert loaded is not None
    assert loaded.content == "v2"
    assert loaded.offset_ms == 2_500


async def test_get_prefers_lrc_over_plain_and_filters_language(
    repo: LyricsRepository, database: Database
) -> None:
    track_id = await create_track(database)
    await repo.save(track_id, language="en", format="plain", content="plain en")
    await repo.save(track_id, language="en", format="lrc", content="lrc en")
    await repo.save(track_id, language="ru", format="plain", content="plain ru")
    await repo.save(track_id, language="ru", format="lrc", content="lrc ru")

    best = await repo.get(track_id)
    assert best is not None
    assert best.format == "lrc"
    assert best.language == "ru"  # newest among the LRC variants

    assert (await repo.get(track_id, "en")).format == "lrc"  # type: ignore[union-attr]
    assert (await repo.get(track_id, "RU")).language == "ru"  # type: ignore[union-attr]
    assert await repo.get(track_id, "de") is None

    # Priority is by format, not insertion order: plain last still loses.
    other = await create_track(database, "Other")
    await repo.save(other, language="und", format="lrc", content=LRC_RU)
    await repo.save(other, language="und", format="plain", content=PLAIN_RU)
    other_best = await repo.get(other)
    assert other_best is not None and other_best.format == "lrc"

    # Plain-only track falls back to plain.
    plain_only = await create_track(database, "Plain only")
    await repo.save(plain_only, language="und", format="plain", content=PLAIN_RU)
    plain_record = await repo.get(plain_only)
    assert plain_record is not None and plain_record.format == "plain"


async def test_set_offset_clamps_updates_all_variants_and_reports_missing(
    repo: LyricsRepository, database: Database
) -> None:
    track_id = await create_track(database)
    await repo.save(track_id, language="ru", format="lrc", content=LRC_RU)
    await repo.save(track_id, language="en", format="plain", content=PLAIN_RU)
    untouched = await create_track(database, "Untouched")
    await repo.save(untouched, language="ru", format="plain", content=PLAIN_RU)

    assert await repo.set_offset(track_id, 1_500) is True
    rows = await database.fetchall(
        "SELECT language, offset_ms FROM lyrics WHERE track_id = ? ORDER BY language",
        (track_id,),
    )
    assert [(row["language"], row["offset_ms"]) for row in rows] == [
        ("en", 1_500),
        ("ru", 1_500),
    ]
    assert await database.scalar(
        "SELECT offset_ms FROM lyrics WHERE track_id = ?", (untouched,)
    ) == 0

    assert await repo.set_offset(track_id, -999_999) is True
    assert await database.scalar(
        "SELECT offset_ms FROM lyrics WHERE track_id = ?", (track_id,)
    ) == -OFFSET_LIMIT_MS

    assert await repo.set_offset(track_id, 999_999) is True
    assert await database.scalar(
        "SELECT offset_ms FROM lyrics WHERE track_id = ?", (track_id,)
    ) == OFFSET_LIMIT_MS

    empty_track = await create_track(database, "No lyrics")
    assert await repo.set_offset(empty_track, 100) is False
    assert await repo.set_offset(999_999, 100) is False


async def test_delete_removes_every_variant(repo: LyricsRepository, database: Database) -> None:
    track_id = await create_track(database)
    await repo.save(track_id, language="ru", format="lrc", content=LRC_RU)
    await repo.save(track_id, language="ru", format="plain", content=PLAIN_RU)
    assert await row_count(database, track_id) == 2

    assert await repo.delete(track_id) is True
    assert await row_count(database, track_id) == 0
    assert await repo.get(track_id) is None
    assert await repo.delete(track_id) is False


async def test_save_rejects_unknown_track_and_invalid_format(
    repo: LyricsRepository, database: Database
) -> None:
    with pytest.raises(NotFoundError):
        await repo.save(999_999, format="lrc", content=LRC_RU)

    track_id = await create_track(database)
    with pytest.raises(ValueError):
        await repo.save(track_id, format="json", content="{}")  # type: ignore[arg-type]
    assert await row_count(database, track_id) == 0


async def test_track_delete_cascades_to_lyrics(
    repo: LyricsRepository, database: Database
) -> None:
    track_id = await create_track(database)
    await repo.save(track_id, language="ru", format="lrc", content=LRC_RU)

    async with database.transaction() as conn:
        await conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))

    assert await row_count(database, track_id) == 0


def test_offset_and_language_helpers() -> None:
    assert clamp_offset(0) == 0
    assert clamp_offset(1_234) == 1_234
    assert clamp_offset(-OFFSET_LIMIT_MS - 1) == -OFFSET_LIMIT_MS
    assert clamp_offset(OFFSET_LIMIT_MS + 1) == OFFSET_LIMIT_MS
    assert normalize_language(None) == "und"
    assert normalize_language("  RU ") == "ru"
    assert normalize_language("") == "und"
    assert set(LYRICS_FORMATS) == {"lrc", "plain"}
