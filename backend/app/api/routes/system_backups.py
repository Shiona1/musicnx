"""Backups, integrity and maintenance — P8, frozen contract in docs/API.md §12.

Read-only endpoints (listing, integrity) stay open on the LAN; every mutation
(create a backup, run maintenance) goes through ``require_mutation_access``:
a valid PIN session, or loopback when no PIN is configured.

Services are looked up on the application context as ``ctx.backups`` /
``ctx.maintenance`` (with ``ctx.backup_service`` / ``ctx.maintenance_service``
as aliases). When the shared context has not wired them yet, the router builds
a stateless service from ``ctx.settings`` and ``ctx.db``, so mounting this
router does not require an ordering change. A missing/disconnected database is
reported as ``503 unavailable`` instead of a misleading empty list.

Mounting (app/main.py, shared file — intentionally not edited here)::

    app.include_router(system_backups.router, prefix="/api/v1")
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_context, require_mutation_access
from app.config import Settings
from app.core.models import BackupInfo, BackupPage, IntegrityResult, MaintenanceResult
from app.database.connection import Database
from app.errors import UnavailableError
from app.maintenance.backup import BackupError, BackupService
from app.maintenance.service import MaintenanceService

router = APIRouter(prefix="/system", tags=["system"])


def _backups(request: Request) -> BackupService:
    context = get_context(request)
    service = getattr(context, "backups", None)
    if service is None:
        service = getattr(context, "backup_service", None)
    if service is not None:
        return service
    db = _database(context)
    return BackupService(_settings(context), db)


def _maintenance(request: Request) -> MaintenanceService:
    context = get_context(request)
    service = getattr(context, "maintenance", None)
    if service is None:
        service = getattr(context, "maintenance_service", None)
    if service is not None:
        return service
    db = _database(context)
    return MaintenanceService(_settings(context), db)


def _database(context: object) -> Database:
    db = getattr(context, "db", None)
    if db is None or not getattr(db, "is_connected", False):
        raise UnavailableError("database is not available")
    return db


def _settings(context: object) -> Settings:
    settings = getattr(context, "settings", None)
    if settings is None:  # pragma: no cover - the lifespan always sets it
        raise UnavailableError("application settings are not available")
    return settings


@router.get("/backups", response_model=BackupPage)
async def list_backups(request: Request) -> BackupPage:
    """Stored ``musicnx-*.db`` files, newest first; ``verified`` stays unknown."""

    return await _backups(request).list_backups()


@router.post(
    "/backups",
    status_code=201,
    response_model=BackupInfo,
    dependencies=[Depends(require_mutation_access)],
)
async def create_backup(request: Request) -> BackupInfo:
    """Copy the live database with the SQLite backup API and verify it."""

    try:
        return await _backups(request).create_backup()
    except BackupError as exc:
        raise UnavailableError(str(exc)) from exc


@router.get("/integrity", response_model=IntegrityResult)
async def integrity(request: Request) -> IntegrityResult:
    """``PRAGMA quick_check`` plus the applied schema version."""

    return await _maintenance(request).integrity()


@router.post(
    "/maintenance",
    response_model=MaintenanceResult,
    dependencies=[Depends(require_mutation_access)],
)
async def run_maintenance(request: Request) -> MaintenanceResult:
    """Remove stale temporaries, checkpoint the WAL and re-check integrity."""

    return await _maintenance(request).maintenance()
