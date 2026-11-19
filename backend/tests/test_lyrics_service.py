"""LyricsService behaviour: cache, local ``.lrc``, Yandex fallback and logs.

No test touches the network: the Yandex client and the HTTP download are fakes;
the database and the repository are the real SQLite ones from the migrations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from app.core.event_bus import EventBus
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.repositories import LibraryRepository, TrackRepository
from app.database.seed import seed_mock_catalog
from app.errors import ConflictError, NotFoundError
from app.lyrics.repository import LyricsRepository
from app.lyrics.service import DEFAULT_TIMEOUT_S, LyricsService
from tests.conftest import make_settings

LRC_TEXT = (
    "[ti:Test song]\n"
    "[offset:0]\n"
    "[00:01.00]First line\n"
    "[00:03.50]Second line\n"
)
DOWNLOADS: dict[str, str] = {
    "https://cdn.test/lyrics/lrc": LRC_TEXT,
    "https://cdn.test/lyrics/text": "Plain first line\nPlain second line",
    "https://cdn.test/lyrics/untimed-lrc": "no timestamps here",
}


class FakeYandexAdapter:
    """Duck-typed stand-in for the frozen adapter (never used for lyrics)."""

    mode = "mock"

    async def close(self) -> None:  # pragma: no cover - shutdown symmetry
        return None


class FakeAuth:
    """Auth manager fake exposing ``get_client`` and counting calls."""

    def __init__(self, client: Any | None) -> None:
        self._client = client
        self.calls = 0

    async def get_client(self) -> Any:
        self.calls += 1
        if self._client is None:
            raise AssertionError("Yandex must not be queried in this test")
        return self._client


class FakeTrackLyrics:
    """Minimal ``TrackLyrics``: download URL plus the library's fallback method."""

    def __init__(self, download_url: str | None = None, text: str | None = None) -> None:
        self.download_url = download_url
        self._text = text

    async def fetch_lyrics_async(self) -> str | None:
        return self._text


class FakeLyricsClient:
    """``tracks_lyrics`` fake returning canned documents per format."""

    def __init__(self, documents: dict[str, FakeTrackLyrics | None]) -> None:
        self._documents = documents
        self.calls: list[tuple[str, str]] = []

    async def tracks_lyrics(self, track_id: str, format_: str = "TEXT", **_: Any):
        self.calls.append((str(track_id), format_))
        return self._documents.get(format_)


class RecordingHttp:
    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.urls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.urls.append(url)
        return self._responses[url]


class FailingHttp:
    def __init__(self, message: str) -> None:
        self.message = message

    async def __call__(self, url: str) -> str:
        raise RuntimeError(self.message)


@pytest.fixture
async def db_factory(tmp_path: Path) -> AsyncIterator[Callable[..., Awaitable[Any]]]:
    """Create a real migrated+seeded database per requested mode."""

    databases: list[Database] = []

    async def make(mode: str = "real") -> tuple[Any, Database]:
        settings = make_settings(tmp_path, mode=mode)
        database = Database(settings.paths.database)
        await database.connect()
        await apply_migrations(database)
        await seed_mock_catalog(database)
        databases.append(database)
        return settings, database

    try:
        yield make
    finally:
        for database in databases:
            await database.close()


def build_service(
    settings: Any,
    database: Database,
    *,
    client: Any | None = None,
    http_fetch: Callable[[str], Awaitable[str]] | None = None,
    lrclib_fetch: Callable[..., Awaitable[dict[str, str] | None]] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[LyricsService, FakeAuth, EventBus, TrackRepository]:
    tracks = TrackRepository(database)
    bus = EventBus()
    auth = FakeAuth(client)
    if lrclib_fetch is None:

        async def no_lrclib(*_args: Any, **_kwargs: Any) -> None:
            return None

        lrclib_fetch = no_lrclib
    service = LyricsService(
        settings,
        database,
        tracks,
        FakeYandexAdapter(),
        auth,
        bus,
        http_fetch=http_fetch,
        lrclib_fetch=lrclib_fetch,
        timeout_s=timeout_s,
    )
    return service, auth, bus, tracks


async def add_yandex_source(
    database: Database, track_id: int, external_id: str = "987654:321"
) -> None:
    async with database.transaction() as conn:
        await conn.execute("INSERT OR IGNORE INTO sources (name) VALUES ('yandex')")
        cursor = await conn.execute("SELECT id FROM sources WHERE name = 'yandex'")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        await conn.execute(
            "INSERT OR IGNORE INTO track_sources "
            "(track_id, source_id, external_id, availability) VALUES (?, ?, ?, 'available')",
            (track_id, int(row["id"]), external_id),
        )


async def register_media_file(
    database: Database, track_id: int, media: Path, lrc_text: str | None = None
) -> None:
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"\x00")
    if lrc_text is not None:
        media.with_suffix(".lrc").write_text(lrc_text, encoding="utf-8")
    await LibraryRepository(database).register_downloaded_file(
        track_id, path=str(media), format=media.suffix.lstrip("."), size_bytes=1
    )


# --------------------------------------------------------------------- mock mode


async def test_mock_mode_returns_deterministic_three_line_lrc(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("mock")
    service, auth, bus, _ = build_service(settings, database)

    dto = await service.get(1)

    assert dto.track_id == 1
    assert dto.available is True
    assert dto.format == "lrc"
    assert dto.source == "mock"
    assert dto.language == "und"
    assert dto.offset_ms == 0
    assert dto.plain is None
    assert [line.time_ms for line in dto.lines] == [0, 4000, 9000]
    assert dto.lines[0].text != ""
    assert auth.calls == 0

    record = await LyricsRepository(database).get(1)
    assert record is not None
    assert record.source == "mock"
    assert record.format == "lrc"
    assert "[00:09.00]" in record.content

    # Deterministic: the same track yields the same sample on every call.
    assert await service.get(1) == dto


async def test_mock_language_argument_is_cached_per_language(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("mock")
    service, _, _, _ = build_service(settings, database)

    dto = await service.get(1, language="ru")

    assert dto.language == "ru"
    record = await LyricsRepository(database).get(1, "ru")
    assert record is not None
    assert record.language == "ru"


# ------------------------------------------------------------------- local files


async def test_local_lrc_beats_yandex_and_mock(
    db_factory: Callable[..., Awaitable[Any]], tmp_path: Path
) -> None:
    settings, database = await db_factory("real")
    await register_media_file(database, 1, tmp_path / "library" / "Artist" / "Song.mp3", LRC_TEXT)
    service, auth, _, _ = build_service(settings, database)

    dto = await service.get(1)

    assert dto.available is True
    assert dto.source == "lrc"
    assert dto.format == "lrc"
    assert [line.text for line in dto.lines] == ["First line", "Second line"]
    assert dto.lines[1].time_ms == 3500
    assert auth.calls == 0

    record = await LyricsRepository(database).get(1)
    assert record is not None
    assert record.source == "lrc"


async def test_local_untimed_file_falls_back_to_plain(
    db_factory: Callable[..., Awaitable[Any]], tmp_path: Path
) -> None:
    settings, database = await db_factory("real")
    await register_media_file(
        database, 1, tmp_path / "library" / "Artist" / "Song.flac", "Just plain words"
    )
    service, _, _, _ = build_service(settings, database)

    dto = await service.get(1)

    assert dto.format == "plain"
    assert dto.plain == "Just plain words"
    assert dto.lines == []
    assert dto.source == "lrc"


# ------------------------------------------------------------------ Yandex path


async def test_yandex_lrc_is_downloaded_and_cached(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    client = FakeLyricsClient({"LRC": FakeTrackLyrics("https://cdn.test/lyrics/lrc")})
    http = RecordingHttp(DOWNLOADS)
    service, auth, _, _ = build_service(settings, database, client=client, http_fetch=http)

    dto = await service.get(1, language="ru")

    assert dto.available is True
    assert dto.source == "yandex"
    assert dto.format == "lrc"
    assert dto.language == "ru"
    assert "https://cdn.test/lyrics/lrc" in http.urls
    assert client.calls == [("987654", "LRC")]
    assert [line.time_ms for line in dto.lines] == [1000, 3500]
    assert auth.calls == 1

    record = await LyricsRepository(database).get(1, "ru")
    assert record is not None
    assert record.source == "yandex"
    assert record.content == LRC_TEXT


async def test_second_get_uses_cache_without_new_network_calls(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    client = FakeLyricsClient({"LRC": FakeTrackLyrics("https://cdn.test/lyrics/lrc")})
    http = RecordingHttp(DOWNLOADS)
    service, auth, _, _ = build_service(settings, database, client=client, http_fetch=http)

    first = await service.get(1)
    second = await service.get(1)

    assert first == second
    assert auth.calls == 1
    assert client.calls == [("987654", "LRC")]
    assert http.urls == ["https://cdn.test/lyrics/lrc"]


async def test_yandex_text_fallback_when_lrc_is_absent(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    client = FakeLyricsClient({"LRC": None, "TEXT": FakeTrackLyrics("https://cdn.test/lyrics/text")})
    http = RecordingHttp(DOWNLOADS)
    service, _, _, _ = build_service(settings, database, client=client, http_fetch=http)

    dto = await service.get(1)

    assert dto.available is True
    assert dto.format == "plain"
    assert dto.lines == []
    assert dto.plain == "Plain first line\nPlain second line"
    assert dto.source == "yandex"
    assert client.calls == [("987654", "LRC"), ("987654", "TEXT")]


async def test_untimed_lrc_endpoint_falls_through_to_text(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    client = FakeLyricsClient(
        {
            "LRC": FakeTrackLyrics("https://cdn.test/lyrics/untimed-lrc"),
            "TEXT": FakeTrackLyrics("https://cdn.test/lyrics/text"),
        }
    )
    http = RecordingHttp(DOWNLOADS)
    service, _, _, _ = build_service(settings, database, client=client, http_fetch=http)

    dto = await service.get(1)

    assert dto.format == "plain"
    assert dto.plain == "Plain first line\nPlain second line"


async def test_library_fallback_method_is_used_when_download_url_missing(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    document = FakeTrackLyrics(text=LRC_TEXT)
    client = FakeLyricsClient({"LRC": document})
    http = RecordingHttp({})
    service, _, _, _ = build_service(settings, database, client=client, http_fetch=http)

    dto = await service.get(1)

    assert dto.available is True
    assert dto.format == "lrc"
    assert http.urls == []


async def test_lrclib_fallback_when_yandex_has_no_lyrics(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    settings.lyrics.lrclib_enabled = True
    calls: list[tuple[Any, ...]] = []

    async def lrclib_fetch(*args: Any) -> dict[str, str]:
        calls.append(args)
        return {"syncedLyrics": LRC_TEXT}

    service, auth, _, _ = build_service(
        settings, database, lrclib_fetch=lrclib_fetch
    )

    dto = await service.get(1)

    assert dto.available is True
    assert dto.format == "lrc"
    assert dto.source == "lrclib"
    assert dto.lines[0].text == "First line"
    assert auth.calls == 0  # no Yandex source on this track: local -> LRCLIB
    assert calls and calls[0][0]  # artist argument was provided


async def test_lrclib_plain_lyrics_are_accepted(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    settings.lyrics.lrclib_enabled = True

    async def lrclib_fetch(*_args: Any) -> dict[str, str]:
        return {"plainLyrics": "Plain one\nPlain two"}

    service, _, _, _ = build_service(
        settings, database, lrclib_fetch=lrclib_fetch
    )

    dto = await service.get(1)

    assert dto.available is True
    assert dto.format == "plain"
    assert dto.source == "lrclib"
    assert dto.plain == "Plain one\nPlain two"


async def test_lrclib_is_skipped_when_disabled(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    settings.lyrics.lrclib_enabled = False
    called = False

    async def lrclib_fetch(*_args: Any) -> dict[str, str]:
        nonlocal called
        called = True
        return {"syncedLyrics": LRC_TEXT}

    service, _, _, _ = build_service(
        settings, database, lrclib_fetch=lrclib_fetch
    )

    dto = await service.get(1)

    assert dto.available is False
    assert called is False


async def test_missing_lyrics_returns_unavailable_and_publishes(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    service, auth, bus, _ = build_service(settings, database)
    subscription = bus.subscribe()
    try:
        dto = await service.get(1)
        event = subscription.queue.get_nowait()
    finally:
        bus.unsubscribe(subscription)

    assert dto.available is False
    assert dto.format == "none"
    assert dto.lines == []
    assert dto.plain is None
    assert auth.calls == 0
    assert event["type"] == "lyrics.updated"
    assert event["payload"] == {"track_id": 1, "available": False}
    assert await LyricsRepository(database).get(1) is None


async def test_fresh_lyrics_are_announced_on_the_bus(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("mock")
    service, _, bus, _ = build_service(settings, database)
    subscription = bus.subscribe()
    try:
        await service.get(1)
        event = subscription.queue.get_nowait()
    finally:
        bus.unsubscribe(subscription)

    assert event["type"] == "lyrics.updated"
    assert event["payload"] == {"track_id": 1, "available": True}


async def test_yandex_timeout_degrades_to_unavailable(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    client = FakeLyricsClient({"LRC": FakeTrackLyrics("https://cdn.test/lyrics/lrc")})

    async def slow_fetch(url: str) -> str:
        await asyncio.sleep(5)
        return LRC_TEXT

    service, _, _, _ = build_service(
        settings, database, client=client, http_fetch=slow_fetch, timeout_s=0.05
    )

    dto = await asyncio.wait_for(service.get(1), timeout=2)

    assert dto.available is False
    assert dto.format == "none"


# --------------------------------------------------------------- offset handling


async def test_set_offset_clamps_and_persists(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("mock")
    service, _, _, _ = build_service(settings, database)
    await service.get(1)

    dto = await service.set_offset(1, 250)
    assert dto.offset_ms == 250
    record = await LyricsRepository(database).get(1)
    assert record is not None
    assert record.offset_ms == 250

    assert (await service.set_offset(1, 999_999)).offset_ms == 60_000
    assert (await service.set_offset(1, -999_999)).offset_ms == -60_000
    record = await LyricsRepository(database).get(1)
    assert record is not None
    assert record.offset_ms == -60_000

    # The cached DTO reports the stored offset on the next read.
    assert (await service.get(1)).offset_ms == -60_000


async def test_set_offset_without_lyrics_is_a_conflict(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("real")
    service, _, _, _ = build_service(settings, database)

    with pytest.raises(ConflictError) as excinfo:
        await service.set_offset(1, 100)

    assert excinfo.value.code == "lyrics_unavailable"


async def test_unknown_track_raises_not_found(
    db_factory: Callable[..., Awaitable[Any]]
) -> None:
    settings, database = await db_factory("mock")
    service, _, _, _ = build_service(settings, database)

    with pytest.raises(NotFoundError):
        await service.get(999_999)
    with pytest.raises(NotFoundError):
        await service.set_offset(999_999, 100)


# --------------------------------------------------------------------- logging


async def test_logs_never_contain_lyrics_text_or_failure_secrets(
    db_factory: Callable[..., Awaitable[Any]], caplog: pytest.LogCaptureFixture
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    secret_lyrics = "[00:01.00]TOP-SECRET-LYRIC"
    secret_token = "SECRET-TOKEN-1234"
    client = FakeLyricsClient({"LRC": FakeTrackLyrics("https://cdn.test/lyrics/lrc")})
    service, _, _, _ = build_service(
        settings,
        database,
        client=client,
        http_fetch=FailingHttp(f"token={secret_token}"),
    )

    caplog.set_level(logging.DEBUG, logger="lyrics.service")
    dto = await service.get(1)

    assert dto.available is False
    assert secret_token not in caplog.text
    assert secret_lyrics not in caplog.text


async def test_success_log_has_no_lyrics_text(
    db_factory: Callable[..., Awaitable[Any]], caplog: pytest.LogCaptureFixture
) -> None:
    settings, database = await db_factory("real")
    await add_yandex_source(database, 1)
    client = FakeLyricsClient({"LRC": FakeTrackLyrics("https://cdn.test/lyrics/lrc")})
    http = RecordingHttp({"https://cdn.test/lyrics/lrc": "[00:01.00]TOP-SECRET-LYRIC"})
    service, _, _, _ = build_service(settings, database, client=client, http_fetch=http)

    caplog.set_level(logging.DEBUG, logger="lyrics.service")
    dto = await service.get(1)

    assert dto.available is True
    assert "TOP-SECRET-LYRIC" not in caplog.text
    assert "cdn.test" not in caplog.text
