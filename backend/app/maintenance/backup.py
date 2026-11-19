"""SQLite backups for MusicNX (P8, docs/API.md §12).

Backups are produced with the SQLite online backup API — never by copying an
active database file — and are verified with ``PRAGMA quick_check`` before the
service reports success. The implementation follows three rules:

* naming — only UTC ``musicnx-YYYYmmdd-HHMMSS.db`` files are created, and every
  name is re-validated before it is turned into a path (no traversal);
* blocking work — the copy and the verification run in ``asyncio.to_thread``
  (AGENTS.md §6); only the live-connection checkpoint is awaited;
* no silent rotation — old backups are never deleted automatically; removing
  them stays an explicit operator action.

Broken or half-written backup files are never returned as valid: they are
removed by the create path and reported as unavailable instead.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from app.config import Settings
from app.core.models import BackupInfo, BackupPage
from app.core.timeutils import to_iso
from app.database.connection import Database
from app.logging_setup import get_logger

logger = get_logger("maintenance.backup")

BACKUP_PREFIX: Final = "musicnx-"
BACKUP_SUFFIX: Final = ".db"
#: Manual backups such as ``musicnx-pre-p4-20261114-212731.db`` are valid.
_SAFE_NAME: Final = re.compile(r"^musicnx-[A-Za-z0-9][A-Za-z0-9._-]*\.db$")
_MAX_NAME_LENGTH: Final = 128
_MAX_COLLISIONS: Final = 100
_CHECKPOINT_MODES: Final = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})


class BackupError(RuntimeError):
    """A backup could not be created or verified."""


def readonly_uri(path: Path) -> str:
    """Return a ``file:`` URI that opens *path* read-only.

    ``Path.as_uri`` percent-encodes the platform path, which keeps the URI
    valid on Windows and for directories containing spaces or ``#``.
    """

    return f"{path.resolve().as_uri()}?mode=ro"


async def wal_checkpoint(db: Database | None, mode: str) -> bool:
    """Run ``PRAGMA wal_checkpoint(<mode>)`` on the live connection.

    ``FULL`` drains the WAL before a backup; ``TRUNCATE`` also shrinks the
    ``-wal`` file during maintenance. Returns ``False`` when there is no live
    connection, another reader kept the checkpoint busy, or the pragma failed —
    a backup is still consistent without it (SQLite reads the WAL itself).
    """

    if mode not in _CHECKPOINT_MODES:
        raise ValueError("unsupported checkpoint mode")
    if db is None or not db.is_connected:
        return False
    cursor = None
    try:
        cursor = await db.connection.execute(f"PRAGMA wal_checkpoint({mode})")
        row = await cursor.fetchone()
    except Exception as exc:  # aiosqlite/sqlite3 errors and closed-connection races
        logger.warning("WAL checkpoint (%s) failed: %s", mode, exc)
        return False
    finally:
        if cursor is not None:
            try:
                await cursor.close()
            except Exception:  # pragma: no cover - connection already gone
                logger.debug("could not close checkpoint cursor", exc_info=True)
    return bool(row) and int(row[0]) == 0


class BackupService:
    """List, create and verify database backups in ``settings.paths.backups``."""

    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self._settings = settings
        self._db = db
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        return self._settings.paths.backups.resolve()

    def path_for(self, name: str) -> Path:
        """Resolve a validated backup *name*; raises ``ValueError`` otherwise."""

        safe = self._validate_name(name)
        directory = self.directory
        path = (directory / safe).resolve()
        if path.parent != directory:
            raise ValueError("backup name escapes the backup directory")
        return path

    async def list_backups(self) -> BackupPage:
        """Newest first; other files and directories in the folder are ignored."""

        directory = self.directory
        entries = await asyncio.to_thread(_list_entries, directory)
        items = [
            BackupInfo(
                name=name,
                size_bytes=size,
                created_at=to_iso(moment),
                verified=None,
            )
            for name, size, moment in entries
        ]
        return BackupPage(items=items, total=len(items), directory=str(directory))

    async def create_backup(self) -> BackupInfo:
        """Copy the live database with the SQLite backup API and verify it."""

        source = self._database_path()
        clock = datetime.now(UTC)
        async with self._lock:
            # Drain the WAL first: a stale -wal is not copied by the backup API.
            await wal_checkpoint(self._db, "FULL")
            try:
                name, size, moment = await asyncio.to_thread(
                    _create_backup_sync, source, self.directory, clock
                )
            except BackupError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise BackupError(f"cannot create backup: {exc}") from exc
        logger.info("database backup created: %s (%s bytes)", name, size)
        return BackupInfo(
            name=name,
            size_bytes=size,
            created_at=to_iso(moment),
            verified=True,
        )

    async def verify_backup(self, name: str) -> bool:
        """Open a stored backup read-only and run ``PRAGMA quick_check``."""

        path = self.path_for(name)
        return await asyncio.to_thread(_verify_database, path)

    def _database_path(self) -> Path:
        if self._db is not None:
            return self._db.path.resolve()
        return self._settings.paths.database.resolve()

    @staticmethod
    def _validate_name(name: str) -> str:
        candidate = name.strip() if isinstance(name, str) else ""
        if (
            not candidate
            or len(candidate) > _MAX_NAME_LENGTH
            or "\x00" in candidate
            or "/" in candidate
            or "\\" in candidate
            or ".." in candidate
            or candidate != Path(candidate).name
            or _SAFE_NAME.fullmatch(candidate) is None
        ):
            raise ValueError("invalid backup name")
        return candidate


def _candidate_name(clock: datetime, attempt: int) -> str:
    stamp = clock.strftime("%Y%m%d-%H%M%S")
    suffix = "" if attempt == 0 else f"-{attempt}"
    return f"{BACKUP_PREFIX}{stamp}{suffix}{BACKUP_SUFFIX}"


def _list_entries(directory: Path) -> list[tuple[str, int, datetime]]:
    """Blocking scan: only regular ``musicnx-*.db`` files, never symlinks."""

    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    found: list[tuple[str, int, datetime]] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            name = BackupService._validate_name(entry.name)
            if name != entry.name:
                continue
            stat = entry.stat()
        except (OSError, ValueError):
            continue
        found.append(
            (name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=UTC))
        )
    found.sort(key=lambda item: (item[2], item[0]), reverse=True)
    return found


def _create_backup_sync(
    source: Path, directory: Path, clock: datetime
) -> tuple[str, int, datetime]:
    """Blocking create-and-verify; the caller owns ``asyncio.to_thread``."""

    if not source.is_file():
        raise BackupError("database file is missing")
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(_MAX_COLLISIONS):
        name = _candidate_name(clock, attempt)
        target = directory / name
        if target.exists():
            continue
        try:
            _copy_database(source, target)
            if not _verify_database(target):
                raise BackupError("backup verification failed")
            stat = target.stat()
        except BackupError:
            _unlink_quiet(target)
            raise
        except (OSError, sqlite3.Error) as exc:
            _unlink_quiet(target)
            raise BackupError(f"cannot create backup: {exc}") from exc
        return name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    raise BackupError("cannot allocate a unique backup name")


def _copy_database(source: Path, target: Path) -> None:
    """Online copy via ``sqlite3.Connection.backup`` (never a file copy)."""

    source_conn = sqlite3.connect(readonly_uri(source), uri=True, timeout=10.0)
    try:
        source_conn.execute("PRAGMA busy_timeout = 5000")
        target_conn = sqlite3.connect(target, timeout=10.0)
        try:
            source_conn.backup(target_conn)
            target_conn.commit()
            # A standalone file: no -wal/-shm sidecars travel with the backup.
            target_conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _verify_database(path: Path) -> bool:
    """Open *path* read-only and treat anything but a clean check as invalid."""

    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(readonly_uri(path), uri=True, timeout=5.0)
    except (OSError, sqlite3.Error):
        return False
    try:
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error:
            return False
    finally:
        conn.close()
    return len(rows) == 1 and str(rows[0][0]).strip().lower() == "ok"


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best-effort cleanup of a failed write
        logger.warning("could not remove incomplete backup %s", path.name)
