"""Shared pytest fixtures. No network, no mpv, no Raspberry Pi required."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from app.config import (
    AudioSettings,
    LibrarySettings,
    LoggingSettings,
    LyricsSettings,
    MockSettings,
    PathsSettings,
    PlayerSettings,
    SecuritySettings,
    ServerSettings,
    Settings,
)
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.seed import seed_mock_catalog
from app.main import create_app
from fastapi.testclient import TestClient


def make_settings(
    tmp_path: Path,
    *,
    mode: str = "mock",
    seed_catalog: bool = True,
    position_persist_interval_s: float = 7.0,
    position_event_hz: float = 2.0,
    pin: str | None = None,
    pin_max_attempts: int = 5,
    pin_lockout_s: float = 60.0,
    import_roots: list[Path] | None = None,
) -> Settings:
    paths = PathsSettings(
        data_dir=tmp_path / "var",
        database=tmp_path / "var" / "musicnx.db",
        library=tmp_path / "var" / "library",
        downloads=tmp_path / "var" / "downloads",
        cache=tmp_path / "var" / "cache",
        backups=tmp_path / "var" / "backups",
    )
    return Settings(
        mode=mode,
        server=ServerSettings(host="127.0.0.1", port=8080),
        paths=paths,
        audio=AudioSettings(backend="mock"),
        logging=LoggingSettings(file=None, level="WARNING"),
        security=SecuritySettings(
            allowed_hosts=["testserver", "localhost", "127.0.0.1", "::1", "musicnx.local"],
            pin=pin,
            pin_max_attempts=pin_max_attempts,
            pin_lockout_s=pin_lockout_s,
        ),
        mock=MockSettings(seed_catalog=seed_catalog),
        library=LibrarySettings(import_roots=import_roots or []),
        lyrics=LyricsSettings(lrclib_enabled=False),
        player=PlayerSettings(
            position_persist_interval_s=position_persist_interval_s,
            position_event_hz=position_event_hz,
        ),
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    from tests.p2_fixtures import create_library_root

    return create_library_root(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.paths.database)
    await database.connect()
    await apply_migrations(database)
    await seed_mock_catalog(database)
    try:
        yield database
    finally:
        await database.close()
