"""Shared fakes for the Yandex P3 tests: no network, no token, no Raspberry Pi.

The fakes mimic only the fields listed in docs/YANDEX_API.md §2, so they also
double as a guard that the mapper does not reach for undocumented attributes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.integrations.yandex_music import mapper
from app.integrations.yandex_music.base import YandexStreamInfo
from app.integrations.yandex_music.repository import YandexRepository
from app.main import _register_error_handlers
from fastapi import APIRouter, FastAPI


def fake_artist(artist_id: str | int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=artist_id, name=name, cover=None)


def fake_album(
    album_id: str | int,
    title: str,
    *,
    year: int | None = None,
    cover_uri: str | None = None,
    artists: tuple[Any, ...] = (),
    track_count: int | None = None,
    cover: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=album_id,
        title=title,
        year=year,
        cover_uri=cover_uri,
        cover=cover,
        track_count=track_count,
        artists=list(artists),
    )


def fake_track(
    track_id: str | int,
    title: str,
    *,
    artists: tuple[Any, ...] = (),
    albums: tuple[Any, ...] = (),
    duration_ms: int | None = 214_000,
    available: bool | None = True,
    preview_duration_ms: int | None = None,
    cover_uri: str | None = None,
    error: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=track_id,
        title=title,
        artists=list(artists),
        albums=list(albums),
        duration_ms=duration_ms,
        available=available,
        available_for_premium_users=None,
        lyrics_available=None,
        cover_uri=cover_uri,
        preview_duration_ms=preview_duration_ms,
        explicit=None,
        filename=None,
        error=error,
    )


def fake_track_short(track_id: str | int) -> SimpleNamespace:
    return SimpleNamespace(id=track_id, track=None)


def fake_sequence(track: SimpleNamespace, *, liked: bool = False) -> SimpleNamespace:
    return SimpleNamespace(type="music", track=track, liked=liked)


def fake_station_result(
    tracks: list[SimpleNamespace], *, batch_id: str, session_id: str
) -> SimpleNamespace:
    return SimpleNamespace(
        sequence=[fake_sequence(track) for track in tracks],
        batch_id=batch_id,
        radio_session_id=session_id,
    )


def fake_download_info(
    codec: str,
    bitrate_in_kbps: int | None,
    *,
    preview: bool = False,
    direct: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        codec=codec,
        bitrate_in_kbps=bitrate_in_kbps,
        preview=preview,
        direct=direct,
    )


async def seed_online_track(
    db: Database,
    track_id: str | int = 1001,
    album_id: str | int = 5001,
    *,
    duration_ms: int = 214_000,
) -> int:
    """Create a Yandex-sourced track with no media file; returns internal id."""

    normalized = mapper.map_track(
        fake_track(
            track_id,
            "Онлайн-трек",
            artists=(fake_artist(8001, "Mock Artist"),),
            albums=(fake_album(album_id, "Mock Album", year=2024),),
            duration_ms=duration_ms,
        )
    )
    assert normalized is not None
    repository = YandexRepository(db, TrackRepository(db))
    return await repository.upsert_track(normalized)


async def seed_local_track(db: Database, directory: Path, size: int = 2048) -> int:
    """Create a local track with one media file on disk; returns internal id."""

    path = directory / "media" / "local.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb\x90\x00" * (size // 4))
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO tracks (title, has_local_file, has_online_source) "
            "VALUES (?, 1, 0)",
            ("Локальный трек",),
        )
        track_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await conn.execute(
            "INSERT INTO media_files (track_id, role, path, format) "
            "VALUES (?, 'library', ?, 'mp3')",
            (track_id, str(path)),
        )
    return track_id


class FakeStreaming:
    """Minimal stand-in for :class:`YandexStreamingService` in route tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[int | str, bool]] = []

    async def stream_info(
        self, track_ref: int | str, *, force_refresh: bool = False
    ) -> YandexStreamInfo:
        self.calls.append((track_ref, force_refresh))
        url = "https://cdn.example/new" if force_refresh else "https://cdn.example/old"
        return YandexStreamInfo(
            url=url, codec="mp3", bitrate_kbps=320, preview=False
        )


class FakeMockAdapter:
    """Adapter exposing ``stream_info_for_track`` for the mock-tone route test."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def stream_info_for_track(self, track_id: int) -> YandexStreamInfo:
        self.calls.append(track_id)
        return YandexStreamInfo(
            url=f"mock://stream/{track_id}",
            codec="mp3",
            bitrate_kbps=320,
            preview=False,
        )


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class FakeUpstreamResponse:
    def __init__(
        self,
        status: int,
        *,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks or [])
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeUpstreamSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def stream_ctx(settings: Any, db: Database, **extras: Any) -> SimpleNamespace:
    """Build the pieces of AppContext the stream route touches."""

    ctx = SimpleNamespace(
        settings=settings,
        tracks=TrackRepository(db),
        yandex_adapter=None,
        yandex=None,
        yandex_streaming=None,
    )
    for name, value in extras.items():
        setattr(ctx, name, value)
    return ctx


def make_router_app(
    router: APIRouter, settings: Any, ctx: Any, *, prefix: str = "/api/v1"
) -> FastAPI:
    """Mount one real router on a throw-away app with a stub context."""

    app = FastAPI()
    app.state.settings = settings
    app.state.ctx = ctx
    app.include_router(router, prefix=prefix)
    _register_error_handlers(app)
    return app


@asynccontextmanager
async def asgi_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI transport: no sockets, no network."""

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
