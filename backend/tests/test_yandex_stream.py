"""Yandex streaming tests: quality choice, Range routes, refresh retry.

Route tests mount the real routers on a throw-away FastAPI app with a stub
``app.state.ctx`` and use httpx's in-process ASGI transport: no sockets, no
network, no Pi.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.api.routes import stream as stream_routes
from app.config import Settings
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.errors import NoPlayableSourceError, UnavailableError
from app.integrations.yandex_music import mapper
from app.integrations.yandex_music.mapper import NormalizedDownloadInfo
from app.integrations.yandex_music.repository import YandexRepository
from app.integrations.yandex_music.streaming import YandexStreamingService
from app.main import _register_error_handlers
from fastapi import FastAPI
from tests.p3_yandex_fixtures import (
    FakeMockAdapter,
    FakeStreaming,
    FakeUpstreamResponse,
    FakeUpstreamSession,
    fake_album,
    fake_download_info,
    fake_track,
    seed_local_track,
    seed_online_track,
    stream_ctx,
)

# --------------------------------------------------------------- pure helpers


def test_select_download_info_prefers_quality_then_bitrate() -> None:
    infos = [
        NormalizedDownloadInfo(codec="aac", bitrate_kbps=192),
        NormalizedDownloadInfo(codec="mp3", bitrate_kbps=128),
        NormalizedDownloadInfo(codec="mp3", bitrate_kbps=320),
    ]
    assert mapper.select_download_info(infos, "mp3") is infos[2]
    assert mapper.select_download_info(infos, "aac") is infos[0]

    flac_infos = [
        NormalizedDownloadInfo(codec="mp3", bitrate_kbps=320),
        NormalizedDownloadInfo(codec="flac", bitrate_kbps=None),
    ]
    assert mapper.select_download_info(flac_infos, "flac") is flac_infos[1]
    # Unknown quality falls back to the MP3 preference order.
    assert mapper.select_download_info(flac_infos, "unknown") is flac_infos[0]


def test_select_download_info_avoids_preview_unless_only_choice() -> None:
    infos = [
        NormalizedDownloadInfo(codec="mp3", bitrate_kbps=320, preview=True),
        NormalizedDownloadInfo(codec="mp3", bitrate_kbps=64),
    ]
    assert mapper.select_download_info(infos, "mp3") is infos[1]

    only_preview = [NormalizedDownloadInfo(codec="mp3", bitrate_kbps=64, preview=True)]
    selected = mapper.select_download_info(only_preview, "mp3")
    assert selected is not None
    assert selected.preview is True
    assert mapper.select_download_info([], "mp3") is None


def test_map_download_infos_uses_direct_link_url() -> None:
    from types import SimpleNamespace

    mapped = mapper.map_download_infos(
        [
            SimpleNamespace(
                codec="mp3",
                bitrate_in_kbps=320,
                preview=False,
                direct=True,
                direct_link="https://cdn.example/signed.mp3",
            )
        ]
    )
    assert mapped[0].direct == "https://cdn.example/signed.mp3"


def test_direct_link_expiry_parses_signed_timestamp() -> None:
    assert (
        mapper.direct_link_expiry(
            "https://cdn.example/file.mp3?expires_at=1767225600&sig=abc"
        )
        == "2026-01-01T00:00:00.000Z"
    )
    assert mapper.direct_link_expiry("https://cdn.example/file.mp3") is None
    assert mapper.direct_link_expiry("https://cdn.example/file.mp3?expires_at=no") is None


# ------------------------------------------------------------------ service


class _FakeDownloadClient:
    """Records every call; returns pre-baked download infos and links."""

    def __init__(self, infos: list[Any], links: dict[str, str]) -> None:
        self._infos = infos
        self._links = links
        self.calls: list[tuple[str, Any, bool]] = []

    async def download_infos(
        self, track_id: str | int, *, refresh: bool = False
    ) -> list[Any]:
        self.calls.append(("download_infos", track_id, refresh))
        return list(self._infos)

    async def direct_link(self, download_info: Any, *, refresh: bool = False) -> str:
        self.calls.append(("direct_link", download_info.codec, refresh))
        return self._links[download_info.codec]


async def _service(
    db: Database, settings: Settings, client: _FakeDownloadClient
) -> tuple[YandexStreamingService, int]:
    repository = YandexRepository(db, TrackRepository(db))
    normalized = mapper.map_track(
        fake_track(
            1001,
            "Онлайн",
            albums=(fake_album(5001, "Album"),),
        )
    )
    assert normalized is not None
    track_id = await repository.upsert_track(normalized)
    return YandexStreamingService(client, repository, settings), track_id


async def test_stream_info_resolves_best_mp3_link(
    db: Database, settings: Settings
) -> None:
    client = _FakeDownloadClient(
        [
            fake_download_info("aac", 192),
            fake_download_info("mp3", 128),
            fake_download_info("mp3", 320),
        ],
        {"mp3": "https://cdn.example/mp3-320", "aac": "https://cdn.example/aac"},
    )
    service, track_id = await _service(db, settings, client)

    info = await service.stream_info(track_id)
    assert info.url == "https://cdn.example/mp3-320"
    assert info.codec == "mp3"
    assert info.bitrate_kbps == 320
    assert info.preview is False
    assert client.calls[0] == ("download_infos", "1001", False)
    assert client.calls[1] == ("direct_link", "mp3", False)


async def test_stream_info_honours_flac_setting(db: Database, settings: Settings) -> None:
    flac_settings = settings.model_copy(
        update={"yandex": settings.yandex.model_copy(update={"quality": "flac"})}
    )
    client = _FakeDownloadClient(
        [
            fake_download_info("mp3", 320),
            fake_download_info("flac", None),
        ],
        {"flac": "https://cdn.example/flac", "mp3": "https://cdn.example/mp3"},
    )
    service, track_id = await _service(db, flac_settings, client)

    info = await service.stream_info(track_id)
    assert info.codec == "flac"
    assert info.url == "https://cdn.example/flac"


async def test_stream_info_force_refresh_asks_auth_for_new_link(
    db: Database, settings: Settings
) -> None:
    client = _FakeDownloadClient(
        [fake_download_info("mp3", 320)], {"mp3": "https://cdn.example/refreshed"}
    )
    service, track_id = await _service(db, settings, client)

    info = await service.stream_info(track_id, force_refresh=True)
    assert info.url == "https://cdn.example/refreshed"
    assert client.calls[0] == ("download_infos", "1001", True)
    assert client.calls[1] == ("direct_link", "mp3", True)


async def test_stream_info_without_download_info_is_503(
    db: Database, settings: Settings
) -> None:
    client = _FakeDownloadClient([], {})
    service, track_id = await _service(db, settings, client)
    with pytest.raises(UnavailableError) as exc_info:
        await service.stream_info(track_id)
    assert exc_info.value.code == "stream_unavailable"


async def test_stream_info_rejects_track_without_yandex_source(
    db: Database, settings: Settings
) -> None:
    client = _FakeDownloadClient([], {})
    repository = YandexRepository(db, TrackRepository(db))
    service = YandexStreamingService(client, repository, settings)
    with pytest.raises(NoPlayableSourceError) as exc_info:
        await service.stream_info(999_999)
    assert exc_info.value.details["reason"] == "no_online_source"


# -------------------------------------------------------------------- routes


def _make_app(settings: Settings, ctx: Any) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.ctx = ctx
    app.include_router(stream_routes.router, prefix="/api/v1")
    _register_error_handlers(app)
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


async def test_stream_serves_local_file_with_ranges(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    track_id = await seed_local_track(db, tmp_path)
    app = _make_app(settings, stream_ctx(settings, db))

    async with _client(app) as client:
        full = await client.get(f"/api/v1/stream/{track_id}")
        assert full.status_code == 200
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["content-type"] == "audio/mpeg"
        assert len(full.content) == 2048

        ranged = await client.get(
            f"/api/v1/stream/{track_id}", headers={"Range": "bytes=0-99"}
        )
        assert ranged.status_code == 206
        assert ranged.headers["content-range"] == "bytes 0-99/2048"
        assert len(ranged.content) == 100

        suffix = await client.get(
            f"/api/v1/stream/{track_id}", headers={"Range": "bytes=-10"}
        )
        assert suffix.status_code == 206
        assert len(suffix.content) == 10


async def test_stream_mock_mode_serves_generated_tone_with_ranges(
    db: Database, settings: Settings
) -> None:
    track_id = await seed_online_track(db, duration_ms=6_000)
    adapter = FakeMockAdapter()
    app = _make_app(settings, stream_ctx(settings, db, yandex_adapter=adapter))

    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/stream/{track_id}", headers={"Range": "bytes=0-99"}
        )
        assert response.status_code == 206
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["content-range"].startswith("bytes 0-99/")
        assert len(response.content) == 100

        cached = list((settings.paths.cache / "mock_tone").glob("*.wav"))
        assert cached, "the mock tone must be cached on disk"
        assert adapter.calls == [track_id]

        replay = await client.get(f"/api/v1/stream/{track_id}")
        assert replay.status_code == 200
        assert replay.headers["content-type"] == "audio/wav"
        assert len(list((settings.paths.cache / "mock_tone").glob("*.wav"))) == len(
            cached
        )


async def test_stream_proxy_refreshes_expired_link_once(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_id = await seed_online_track(db)
    streaming = FakeStreaming()
    app = _make_app(settings, stream_ctx(settings, db, yandex_streaming=streaming))

    opened: list[tuple[str, str | None]] = []
    sessions: list[FakeUpstreamSession] = []
    responses = [
        FakeUpstreamResponse(403),
        FakeUpstreamResponse(
            200,
            chunks=[b"abc", b"def"],
            headers={"Content-Type": "audio/mpeg", "Content-Length": "6"},
        ),
    ]

    async def fake_open(url: str, range_header: str | None):
        opened.append((url, range_header))
        session = FakeUpstreamSession()
        sessions.append(session)
        return session, responses.pop(0)

    monkeypatch.setattr(stream_routes, "_open_upstream", fake_open)

    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/stream/{track_id}", headers={"Range": "bytes=0-5"}
        )

    assert response.status_code == 200
    assert response.content == b"abcdef"
    assert response.headers["content-type"] == "audio/mpeg"
    assert opened == [
        ("https://cdn.example/old", "bytes=0-5"),
        ("https://cdn.example/new", "bytes=0-5"),
    ]
    assert streaming.calls == [(track_id, False), (track_id, True)]
    assert all(session.closed for session in sessions)


async def test_stream_proxy_forwards_206_and_content_range(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_id = await seed_online_track(db)
    app = _make_app(settings, stream_ctx(settings, db, yandex_streaming=FakeStreaming()))
    session = FakeUpstreamSession()
    response = FakeUpstreamResponse(
        206,
        chunks=[b"abc"],
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": "bytes 0-2/6",
            "Content-Length": "3",
        },
    )

    async def fake_open(url: str, range_header: str | None):
        assert range_header == "bytes=0-2"
        return session, response

    monkeypatch.setattr(stream_routes, "_open_upstream", fake_open)

    async with _client(app) as client:
        result = await client.get(
            f"/api/v1/stream/{track_id}", headers={"Range": "bytes=0-2"}
        )

    assert result.status_code == 206
    assert result.headers["content-range"] == "bytes 0-2/6"
    assert result.headers["accept-ranges"] == "bytes"
    assert result.headers["content-type"] == "audio/mpeg"
    assert result.content == b"abc"
    assert session.closed is True
    assert response.released is True


async def test_stream_proxy_reports_upstream_failure_as_503(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    track_id = await seed_online_track(db)
    app = _make_app(settings, stream_ctx(settings, db, yandex_streaming=FakeStreaming()))

    session = FakeUpstreamSession()

    async def fake_open(url: str, range_header: str | None):
        return session, FakeUpstreamResponse(500)

    monkeypatch.setattr(stream_routes, "_open_upstream", fake_open)

    async with _client(app) as client:
        response = await client.get(f"/api/v1/stream/{track_id}")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "stream_unavailable"
    assert session.closed is True


async def test_stream_unknown_track_is_404(
    db: Database, settings: Settings
) -> None:
    app = _make_app(settings, stream_ctx(settings, db))
    async with _client(app) as client:
        response = await client.get("/api/v1/stream/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "track_not_found"


async def test_stream_without_any_source_is_503(
    db: Database, settings: Settings, tmp_path: Path
) -> None:
    # Metadata-only track: no media file and no adapter to resolve a stream.
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO tracks (title, has_local_file, has_online_source) "
            "VALUES ('Metadata only', 0, 0)"
        )
        track_id = int(cursor.lastrowid or 0)
        await cursor.close()
    app = _make_app(settings, stream_ctx(settings, db))
    async with _client(app) as client:
        response = await client.get(f"/api/v1/stream/{track_id}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "stream_unavailable"


def test_routes_are_mounted_in_main_app(settings: Settings) -> None:
    from app.main import create_app

    app = create_app(settings)
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/api/v1/stream/{track_id}" in paths
    assert "/api/v1/yandex/likes" in paths

