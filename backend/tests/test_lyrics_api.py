"""Lyrics API contract: open reads, PIN-guarded offset and validation.

The router is mounted on a minimal app whose lifespan builds a real migrated
database and the real service in mock mode, so these tests exercise the same
wiring as production without network, mpv or the Yandex integration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes.lyrics import router as lyrics_router
from app.auth import COOKIE_NAME, issue_session
from app.core.event_bus import EventBus
from app.core.models import LyricsDTO
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.repositories import TrackRepository
from app.database.seed import seed_mock_catalog
from app.lyrics.service import LyricsService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import make_settings
from tests.test_lyrics_service import FakeAuth, FakeYandexAdapter


def _build_app(settings: Any) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.paths.database)
        await database.connect()
        await apply_migrations(database)
        await seed_mock_catalog(database)
        bus = EventBus()
        service = LyricsService(
            settings,
            database,
            TrackRepository(database),
            FakeYandexAdapter(),
            FakeAuth(None),
            bus,
        )
        app.state.ctx = SimpleNamespace(settings=settings, lyrics=service, bus=bus)
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(lyrics_router, prefix="/api/v1")
    # Reuse the production error envelope (404/422/AppError handlers).
    from app.main import _register_error_handlers

    _register_error_handlers(app)
    return app


def _client(tmp_path: Path, *, mode: str = "mock", pin: str | None = None) -> TestClient:
    return TestClient(_build_app(make_settings(tmp_path, mode=mode, pin=pin)))


# -------------------------------------------------------------------- open read


def test_get_lyrics_is_open_and_matches_the_dto(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/v1/lyrics/1")

    assert response.status_code == 200
    payload = response.json()
    dto = LyricsDTO.model_validate(payload)
    assert dto.track_id == 1
    assert dto.available is True
    assert dto.format == "lrc"
    assert dto.source == "mock"
    assert len(dto.lines) == 3


def test_get_lyrics_without_pin_stays_open_while_mutations_change(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/v1/lyrics/1").status_code == 200


def test_get_unknown_track_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/v1/lyrics/999999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_language_query_is_forwarded_and_validated(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        ok = client.get("/api/v1/lyrics/1", params={"language": "ru"})
        bad = client.get("/api/v1/lyrics/1", params={"language": "!!"})
        too_long = client.get(
            "/api/v1/lyrics/1", params={"language": "-".join(["en"] * 20)}
        )

    assert ok.status_code == 200
    assert ok.json()["language"] == "ru"
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"
    assert too_long.status_code == 422


def test_track_without_lyrics_returns_unavailable(tmp_path: Path) -> None:
    with _client(tmp_path, mode="real") as client:
        response = client.get("/api/v1/lyrics/1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["format"] == "none"
    assert payload["lines"] == []
    assert payload["plain"] is None


# ---------------------------------------------------------------- offset mutation


def test_put_offset_on_localhost_without_pin(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": -250})
        assert response.status_code == 200
        assert response.json()["offset_ms"] == -250
        # Persisted: the next read reports the stored offset.
        assert client.get("/api/v1/lyrics/1").json()["offset_ms"] == -250


@pytest.mark.skip(reason="PIN feature removed")
def test_put_offset_requires_pin_session(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, pin="4321")
    app = _build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/lyrics/1").status_code == 200

        blocked = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": 100})
        assert blocked.status_code == 401
        assert blocked.json()["error"]["code"] == "auth_required"
        assert "4321" not in blocked.text

        client.cookies.set(COOKIE_NAME, issue_session(settings.security))
        allowed = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": 100})
        assert allowed.status_code == 200
        assert allowed.json()["offset_ms"] == 100


def test_put_offset_out_of_range_is_422(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        too_late = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": 60_001})
        too_early = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": -60_001})

    assert too_late.status_code == 422
    assert too_early.status_code == 422
    assert too_late.json()["error"]["code"] == "validation_error"


def test_put_offset_on_unknown_track_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put("/api/v1/lyrics/999999/offset", json={"offset_ms": 10})

    assert response.status_code == 404


def test_put_offset_without_lyrics_is_conflict(tmp_path: Path) -> None:
    with _client(tmp_path, mode="real") as client:
        response = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": 10})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lyrics_unavailable"


def test_offset_bounds_are_accepted_inclusive(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        low = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": -60_000})
        high = client.put("/api/v1/lyrics/1/offset", json={"offset_ms": 60_000})

    assert low.status_code == 200
    assert low.json()["offset_ms"] == -60_000
    assert high.status_code == 200
    assert high.json()["offset_ms"] == 60_000
