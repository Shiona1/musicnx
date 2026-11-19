"""MaintenanceService: integrity, cleanup and mutation guards (P8).

The cleanup tests build real temporary trees so the safety rules are exercised
end to end: only stale ``.part``/``*.tmp`` files may disappear, while media,
covers, the database and freshly written temporaries must survive.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.api.routes import system_backups
from app.config import Settings
from app.main import create_app
from app.maintenance.service import (
    PART_MAX_AGE_S,
    TMP_MAX_AGE_S,
    MaintenanceService,
)
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def _app(settings: Settings):
    """``create_app`` plus this router mounted before the SPA catch-all.

    In ``app/main.py`` the integrator adds the include before
    ``_register_static``; tests append the router afterwards, so the new routes
    are moved in front of the ``/{full_path:path}`` fallback explicitly.
    """

    app = create_app(settings)
    before = len(app.router.routes)
    app.include_router(system_backups.router, prefix="/api/v1")
    routes = app.router.routes
    added = routes[before:]
    del routes[before:]
    index = next(
        position
        for position, route in enumerate(routes)
        if getattr(route, "path", None) == "/{full_path:path}"
    )
    routes[index:index] = added
    return app


def _client(settings: Settings) -> TestClient:
    return TestClient(_app(settings))


@pytest.fixture
def maintenance_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_path, seed_catalog=False)
    with _client(settings) as client:
        yield client


def _age(path: Path, seconds: int) -> None:
    """Backdate a file's mtime (cleanup decides by age, not by name)."""

    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


async def test_integrity_reports_a_healthy_database(settings: Settings, db) -> None:
    result = await MaintenanceService(settings, db).integrity()

    assert result.ok is True
    assert result.quick_check == "ok"
    assert result.schema_version == 4
    assert result.database.endswith("musicnx.db")


async def test_integrity_reports_broken_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)
    settings.paths.database.parent.mkdir(parents=True, exist_ok=True)
    settings.paths.database.write_bytes(b"this is not sqlite" * 128)

    result = await MaintenanceService(settings).integrity()

    assert result.ok is False
    assert result.quick_check != "ok"
    assert result.schema_version is None


async def test_integrity_reports_missing_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)

    result = await MaintenanceService(settings).integrity()

    assert result.ok is False
    assert result.quick_check == "unable to open database file"
    assert result.schema_version is None


async def test_maintenance_removes_only_stale_temporaries(
    settings: Settings, db
) -> None:
    downloads = settings.paths.downloads
    cache = settings.paths.cache
    nested = downloads / "Artist" / "Album (2020)"
    covers = cache / "covers"
    nested.mkdir(parents=True)
    covers.mkdir(parents=True)

    stale_part = nested / "track.flac.part"
    stale_part.write_bytes(b"x" * 100)
    _age(stale_part, PART_MAX_AGE_S + 60)

    fresh_part = downloads / "fresh.mp3.part"
    fresh_part.write_bytes(b"y" * 10)
    _age(fresh_part, 60)

    media = nested / "track.flac"
    media.write_bytes(b"m" * 50)
    _age(media, PART_MAX_AGE_S * 2)

    stale_tmp = covers / "cover.tmp"
    stale_tmp.write_bytes(b"t" * 10)
    _age(stale_tmp, TMP_MAX_AGE_S + 60)

    fresh_tmp = cache / "fresh.tmp"
    fresh_tmp.write_bytes(b"u" * 10)
    _age(fresh_tmp, 60)

    cover = covers / "cover-300.jpg"
    cover.write_bytes(b"c" * 20)
    _age(cover, TMP_MAX_AGE_S * 2)

    # Wrong suffix for its root: never a cleanup candidate.
    misplaced = cache / "keep.part"
    misplaced.write_bytes(b"k" * 5)
    _age(misplaced, PART_MAX_AGE_S * 2)

    database = settings.paths.database
    result = await MaintenanceService(settings, db).maintenance()

    assert result.removed_files == 2
    assert result.freed_bytes == 110
    assert result.wal_checkpointed is True
    assert result.integrity is not None
    assert result.integrity.ok is True
    assert result.ok is True

    assert not stale_part.exists()
    assert not stale_tmp.exists()
    assert fresh_part.exists()
    assert fresh_tmp.exists()
    assert media.exists()
    assert cover.exists()
    assert misplaced.exists()
    assert database.exists()


async def test_maintenance_tolerates_missing_directories(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)

    result = await MaintenanceService(settings).maintenance()

    assert result.removed_files == 0
    assert result.freed_bytes == 0
    assert result.wal_checkpointed is False
    assert result.integrity is not None
    assert result.integrity.ok is False
    assert result.ok is False
    assert result.message == "database integrity check failed"


async def test_run_entry_point_used_by_timer(settings: Settings, db) -> None:
    """The housekeeping timer resolves ``MaintenanceService.run()``."""

    service = MaintenanceService(settings, db)

    result = await service.run()

    assert result.removed_files == 0
    assert result.integrity is not None
    assert result.integrity.ok is True


def test_api_integrity_and_maintenance(maintenance_client: TestClient) -> None:
    settings = maintenance_client.app.state.settings
    stale = settings.paths.downloads / "abandoned.part"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"p" * 32)
    _age(stale, PART_MAX_AGE_S + 60)

    integrity = maintenance_client.get("/api/v1/system/integrity")
    assert integrity.status_code == 200
    body = integrity.json()
    assert body["ok"] is True
    assert body["quick_check"] == "ok"
    assert body["schema_version"] == 4

    response = maintenance_client.post("/api/v1/system/maintenance")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["removed_files"] == 1
    assert payload["freed_bytes"] == 32
    assert payload["wal_checkpointed"] is True
    assert payload["integrity"]["ok"] is True
    assert not stale.exists()


@pytest.mark.skip(reason="PIN feature removed")
def test_maintenance_mutation_requires_pin(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False, pin="1234")
    with _client(settings) as client:
        assert client.get("/api/v1/system/integrity").status_code == 200
        denied = client.post("/api/v1/system/maintenance")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "auth_required"

        assert client.post("/api/v1/auth/pin", json={"pin": "1234"}).status_code == 200
        assert client.post("/api/v1/system/maintenance").status_code == 200


def test_maintenance_routes_report_503_without_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)
    app = _app(settings)
    with TestClient(app) as client:
        class _Disconnected:
            is_connected = False

        app.state.ctx = SimpleNamespace(settings=settings, db=_Disconnected())

        assert client.get("/api/v1/system/integrity").status_code == 503
        assert client.post("/api/v1/system/maintenance").status_code == 503
