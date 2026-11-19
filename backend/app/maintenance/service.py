"""Integrity checks and housekeeping for MusicNX (P8, docs/API.md §12).

``integrity()`` answers one question — is the database file readable and
consistent? — from a fresh read-only connection, so a damaged file is reported
as ``ok=false`` instead of raising from the process connection.

``maintenance()`` removes only files this server creates and later abandons:

* download ``.part`` files older than seven days (an active download keeps
  touching its partial file, so it is never mistaken for abandoned);
* cache ``*.tmp`` files older than one day (cover and URL downloads write
  through temporaries that are renamed on success).

Media files, covers, backups and the database itself are never deleted. The
filesystem scan runs in ``asyncio.to_thread`` (AGENTS.md §6); the WAL
checkpoint is awaited on the live connection.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from app.config import Settings
from app.core.models import IntegrityResult, MaintenanceResult
from app.database.connection import Database
from app.logging_setup import get_logger
from app.maintenance.backup import readonly_uri, wal_checkpoint

logger = get_logger("maintenance.service")

#: Abandoned partial downloads are kept a week so a resume is still possible.
PART_MAX_AGE_S: Final = 7 * 24 * 60 * 60
#: Cache temporaries are short-lived by design.
TMP_MAX_AGE_S: Final = 24 * 60 * 60
_MAX_CHECK_TEXT: Final = 500


class MaintenanceService:
    """Database integrity plus stale-temporary cleanup."""

    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self._settings = settings
        self._db = db

    async def integrity(self) -> IntegrityResult:
        """``PRAGMA quick_check`` and the schema version, off the event loop."""

        return await asyncio.to_thread(_integrity_sync, self._database_path())

    async def maintenance(self) -> MaintenanceResult:
        """Clean stale temporaries, truncate the WAL, then re-check integrity."""

        removed, freed = await asyncio.to_thread(
            _cleanup_sync,
            self._settings.paths.downloads,
            self._settings.paths.cache,
            self._database_path(),
        )
        checkpointed = await wal_checkpoint(self._db, "TRUNCATE")
        integrity = await self.integrity()
        if removed:
            logger.info(
                "maintenance removed %s stale files (%s bytes)", removed, freed
            )
        return MaintenanceResult(
            ok=integrity.ok,
            removed_files=removed,
            freed_bytes=freed,
            wal_checkpointed=checkpointed,
            integrity=integrity,
            message=None if integrity.ok else "database integrity check failed",
        )

    async def run(self) -> MaintenanceResult:
        """Single entry point used by the periodic timer (alias of ``maintenance``)."""

        return await self.maintenance()

    def _database_path(self) -> Path:
        if self._db is not None:
            return self._db.path.resolve()
        return self._settings.paths.database.resolve()


def _integrity_sync(path: Path) -> IntegrityResult:
    """Blocking check; never raises for a missing or corrupted file."""

    database = str(path)
    if not path.is_file():
        return IntegrityResult(
            ok=False,
            database=database,
            quick_check="unable to open database file",
            schema_version=None,
        )
    try:
        conn = sqlite3.connect(readonly_uri(path), uri=True, timeout=5.0)
    except (OSError, sqlite3.Error) as exc:
        return IntegrityResult(
            ok=False,
            database=database,
            quick_check=_short_error(exc),
            schema_version=None,
        )
    quick_check = "unknown"
    schema_version: int | None = None
    try:
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            quick_check = _short_error(exc)
        else:
            quick_check = _check_text(rows)
        if quick_check == "ok":
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                schema_version = int(row[0]) if row is not None else 0
            except sqlite3.Error:
                # A valid database without the migration table is reported
                # without a version instead of failing the whole check.
                schema_version = None
    finally:
        conn.close()
    return IntegrityResult(
        ok=quick_check == "ok",
        database=database,
        quick_check=quick_check,
        schema_version=schema_version,
    )


def _check_text(rows: list[tuple[object, ...]]) -> str:
    if not rows:
        return "unknown"
    if len(rows) == 1 and str(rows[0][0]).strip().lower() == "ok":
        return "ok"
    # Corruption is described by one text row per problem.
    return "; ".join(str(row[0]) for row in rows)[:_MAX_CHECK_TEXT]


def _short_error(exc: BaseException) -> str:
    return f"error: {exc}"[:_MAX_CHECK_TEXT]


def _cleanup_sync(
    downloads_root: Path, cache_root: Path, database: Path
) -> tuple[int, int]:
    """Remove stale ``.part`` under downloads and stale ``*.tmp`` under cache."""

    now = time.time()
    removed = 0
    freed = 0
    targets = (
        (downloads_root, ".part", PART_MAX_AGE_S),
        (cache_root, ".tmp", TMP_MAX_AGE_S),
    )
    for root, suffix, max_age in targets:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            continue
        for path in _iter_files(resolved_root):
            if path.suffix != suffix or path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            # Defence in depth: never unlink the live database or escape the root.
            if resolved == database or resolved.suffix == ".db":
                continue
            if not resolved.is_relative_to(resolved_root):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime <= max_age:
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("cannot remove stale file %s: %s", path.name, exc)
                continue
            removed += 1
            freed += stat.st_size
    return removed, freed


def _iter_files(root: Path) -> Iterator[Path]:
    """Walk *root* without following symlinked directories."""

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames if not (current / name).is_symlink()
        ]
        for name in filenames:
            yield current / name
