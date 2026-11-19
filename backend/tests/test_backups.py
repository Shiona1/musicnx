"""Backup service and API contract (docs/API.md §12).

Everything uses a temporary data directory: no network, no Raspberry Pi and no
real Media library. The route module is mounted explicitly because the shared
``app/main.py`` wiring is owned by the integrator.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.api.routes import system_backups
from app.config import Settings
from app.main import create_app
from app.maintenance.backup import BackupError, BackupService
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
def backup_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_path, seed_catalog=False)
    with _client(settings) as client:
        yield client


async def test_create_and_list_backup_shape(settings: Settings, db) -> None:
    service = BackupService(settings, db)

    empty = await service.list_backups()
    assert empty.items == []
    assert empty.total == 0
    assert empty.directory == str(settings.paths.backups.resolve())

    created = await service.create_backup()
    assert created.name.startswith("musicnx-")
    assert created.name.endswith(".db")
    assert created.size_bytes > 0
    assert created.created_at.endswith("Z")
    assert created.verified is True

    page = await service.list_backups()
    assert page.total == 1
    item = page.items[0]
    assert item.name == created.name
    assert item.size_bytes == created.size_bytes
    assert item.created_at.endswith("Z")
    # Listing does not open every file, so verification is reported as unknown.
    assert item.verified is None


async def test_backup_is_a_consistent_database(settings: Settings, db) -> None:
    service = BackupService(settings, db)
    created = await service.create_backup()

    path = service.path_for(created.name)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] > 0


async def test_second_backup_gets_a_distinct_name(settings: Settings, db) -> None:
    service = BackupService(settings, db)

    first = await service.create_backup()
    second = await service.create_backup()

    assert first.name != second.name
    assert len(list(settings.paths.backups.glob("musicnx-*.db"))) == 2


async def test_create_backup_does_not_rotate(settings: Settings, db) -> None:
    backups = settings.paths.backups
    backups.mkdir(parents=True)
    keep = backups / "musicnx-20200101-000000.db"
    keep.write_bytes(b"old")

    await BackupService(settings, db).create_backup()

    assert keep.exists()
    names = {item.name for item in (await BackupService(settings).list_backups()).items}
    assert keep.name in names


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "../evil.db",
        "..\\evil.db",
        "/etc/passwd",
        "C:\\evil.db",
        "musicnx-x.db/../evil.db",
        "musicnx-x.db\\evil.db",
        "musicnx-..db",
        "other.db",
        "musicnx-not-a-database.txt",
        "musicnx-.db",
        "musicnx-x.db\x00",
    ],
)
def test_path_for_rejects_unsafe_names(settings: Settings, name: str) -> None:
    with pytest.raises(ValueError):
        BackupService(settings).path_for(name)


def test_path_for_accepts_manual_backup_names(settings: Settings) -> None:
    service = BackupService(settings)

    path = service.path_for("musicnx-pre-p4-20261114-212731.db")

    assert path.name == "musicnx-pre-p4-20261114-212731.db"
    assert path.parent == settings.paths.backups.resolve()


async def test_list_ignores_foreign_files(settings: Settings) -> None:
    backups = settings.paths.backups
    backups.mkdir(parents=True)
    (backups / "musicnx-good.db").write_bytes(b"x")
    (backups / "musicnx-..db").write_bytes(b"x")
    (backups / "notes.txt").write_text("x", encoding="utf-8")
    (backups / "other.db").write_bytes(b"x")
    (backups / "musicnx-directory").mkdir()

    page = await BackupService(settings).list_backups()

    assert [item.name for item in page.items] == ["musicnx-good.db"]


async def test_verify_backup_detects_corruption(settings: Settings, db) -> None:
    service = BackupService(settings, db)

    created = await service.create_backup()
    assert await service.verify_backup(created.name) is True

    broken = settings.paths.backups / "musicnx-broken.db"
    broken.write_bytes(b"definitely not an sqlite database" * 64)
    assert await service.verify_backup(broken.name) is False

    empty = settings.paths.backups / "musicnx-empty.db"
    empty.write_bytes(b"")
    assert await service.verify_backup(empty.name) is False

    assert await service.verify_backup("musicnx-missing.db") is False


async def test_create_backup_reports_missing_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)
    service = BackupService(settings)

    with pytest.raises(BackupError) as excinfo:
        await service.create_backup()

    assert "missing" in str(excinfo.value)
    assert not settings.paths.database.exists()


def test_api_create_and_list(backup_client: TestClient) -> None:
    created = backup_client.post("/api/v1/system/backups")

    assert created.status_code == 201
    payload = created.json()
    assert set(payload) == {"name", "size_bytes", "created_at", "verified"}
    assert payload["name"].startswith("musicnx-")
    assert payload["size_bytes"] > 0
    assert payload["verified"] is True

    listed = backup_client.get("/api/v1/system/backups")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == payload["name"]
    assert body["items"][0]["verified"] is None
    assert body["directory"].endswith("backups")


@pytest.mark.skip(reason="PIN feature removed")
def test_backup_mutations_require_pin(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False, pin="1234")
    with _client(settings) as client:
        # Listing is read-only and stays open on the LAN.
        assert client.get("/api/v1/system/backups").status_code == 200

        denied = client.post("/api/v1/system/backups")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "auth_required"

        login = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert login.status_code == 200
        assert client.post("/api/v1/system/backups").status_code == 201


def test_backup_routes_report_503_without_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)
    app = _app(settings)
    with TestClient(app) as client:
        class _Disconnected:
            is_connected = False

        app.state.ctx = SimpleNamespace(settings=settings, db=_Disconnected())

        assert client.get("/api/v1/system/backups").status_code == 503
        response = client.post("/api/v1/system/backups")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unavailable"


def test_router_prefers_context_service(backup_client: TestClient) -> None:
    from app.core.models import BackupPage

    class _Stub:
        async def list_backups(self) -> BackupPage:
            return BackupPage(items=[], total=0, directory="/wired/elsewhere")

    backup_client.app.state.ctx.backups = _Stub()

    body = backup_client.get("/api/v1/system/backups").json()
    assert body["directory"] == "/wired/elsewhere"
