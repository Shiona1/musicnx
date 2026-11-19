"""SQL repository for the P4 download queue (``download_tasks``).

Migration 0001 is frozen: the table has no ``title``/``quality``/``format``
columns. A ``DownloadTask`` DTO is therefore assembled at read time — ``title``
comes from the owning track row, ``format`` from the extension of
``target_path`` (the UI badge shows the actual container) and ``quality`` stays
``None`` because the queue never promises a quality it cannot prove from
persisted data.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any, cast

import aiosqlite

from app.core.models import DownloadStatus, DownloadTask, DownloadTaskPage
from app.database.connection import Database, Row
from app.errors import NotFoundError

DOWNLOAD_STATUSES: tuple[DownloadStatus, ...] = (
    "queued",
    "downloading",
    "paused",
    "completed",
    "failed",
    "cancelled",
)

# Formats that can be derived from the on-disk extension. ``m4a`` is ambiguous
# (AAC or FLAC-in-MP4), which is exactly why ``format`` is the container and
# ``quality`` is not guessed.
FORMAT_BY_SUFFIX: dict[str, str] = {
    ".flac": "flac",
    ".m4a": "m4a",
    ".mp3": "mp3",
    ".aac": "aac",
    ".wav": "wav",
    ".ogg": "ogg",
    ".opus": "opus",
}

TASK_COLUMNS = """
    d.id,
    d.track_id,
    d.status,
    d.priority,
    d.attempts,
    d.bytes_done,
    d.bytes_total,
    d.target_path,
    d.error,
    d.created_at,
    d.updated_at,
    t.title AS track_title
"""

TASK_FROM = "FROM download_tasks d LEFT JOIN tracks t ON t.id = d.track_id"

# All timestamps are UTC ISO-8601 with milliseconds, matching
# ``app.core.timeutils.to_iso`` used by the rest of the schema.
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _task_sql(where: str) -> str:
    return f"SELECT {TASK_COLUMNS} {TASK_FROM} WHERE {where}"


def download_format(target_path: str | None) -> str | None:
    """Actual container derived from the target file extension."""

    if not target_path:
        return None
    suffix = PurePosixPath(target_path).suffix.lower()
    if not suffix:
        return None
    return FORMAT_BY_SUFFIX.get(suffix) or suffix.lstrip(".")


def row_to_download_task(row: Row) -> DownloadTask:
    """Map one ``download_tasks`` row (joined with its track) to the DTO."""

    return DownloadTask(
        id=int(row["id"]),
        track_id=int(row["track_id"]),
        title=row["track_title"],
        status=cast(DownloadStatus, row["status"]),
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        bytes_done=int(row["bytes_done"]),
        bytes_total=row["bytes_total"],
        target_path=row["target_path"],
        quality=None,
        format=download_format(row["target_path"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def validate_status(status: str) -> DownloadStatus:
    """Reject statuses outside the frozen CHECK constraint with a clear error."""

    if status not in DOWNLOAD_STATUSES:
        raise ValueError(f"invalid download status: {status!r}")
    return cast(DownloadStatus, status)


async def _fetchone(
    conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> Row | None:
    cursor = await conn.execute(sql, parameters)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def _fetchall(
    conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> list[Row]:
    cursor = await conn.execute(sql, parameters)
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


class DownloadRepository:
    """CRUD over ``download_tasks``; the only module that writes download SQL.

    ``track_id`` is UNIQUE in the schema, so a track can never be queued twice:
    ``add`` returns the existing row and a ``created`` flag instead of failing.
    Status transitions are explicit methods rather than free-form updates so the
    scheduler/manager cannot invent states the CHECK constraint rejects.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ reads

    async def get(self, task_id: int) -> DownloadTask | None:
        row = await self._db.fetchone(_task_sql("d.id = ?"), (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def get_by_track(self, track_id: int) -> DownloadTask | None:
        row = await self._db.fetchone(_task_sql("d.track_id = ?"), (track_id,))
        return row_to_download_task(row) if row is not None else None

    async def list(self, status: str | None = None) -> DownloadTaskPage:
        """Queue and history, newest first (docs/API.md §8.2)."""

        if status is not None:
            validate_status(status)
        if status is None:
            rows = await self._db.fetchall(
                f"SELECT {TASK_COLUMNS} {TASK_FROM} ORDER BY d.id DESC"
            )
        else:
            rows = await self._db.fetchall(
                f"SELECT {TASK_COLUMNS} {TASK_FROM} WHERE d.status = ? ORDER BY d.id DESC",
                (status,),
            )
        items = [row_to_download_task(row) for row in rows]
        return DownloadTaskPage(items=items, total=len(items))

    # ----------------------------------------------------------------- writes

    async def add(self, track_id: int, *, priority: int = 0) -> tuple[DownloadTask, bool]:
        """Insert a queued task or return the existing one.

        Returns ``(task, created)``; ``created`` is ``False`` when the track was
        already queued/downloaded and the caller must not enqueue it again.
        """

        try:
            async with self._db.transaction() as conn:
                cursor = await conn.execute(
                    """
                    INSERT OR IGNORE INTO download_tasks (track_id, status, priority)
                    VALUES (?, 'queued', ?)
                    """,
                    (track_id, priority),
                )
                created = (cursor.rowcount or 0) == 1
                await cursor.close()
                row = await self._fetch_task(conn, "d.track_id = ?", (track_id,))
        except aiosqlite.IntegrityError as exc:
            # Unknown track: the foreign key is the authority, no pre-check race.
            raise NotFoundError(
                "Track not found", details={"track_id": track_id}
            ) from exc
        except aiosqlite.Error as exc:  # pragma: no cover - unexpected driver error
            raise RuntimeError(f"cannot queue download: {exc}") from exc
        if row is None:  # pragma: no cover - defensive
            raise NotFoundError("Track not found", details={"track_id": track_id})
        return row_to_download_task(row), created

    async def update_progress(
        self, task_id: int, bytes_done: int, bytes_total: int | None = None
    ) -> None:
        """Persist transfer progress; ``bytes_total`` is kept when unknown."""

        async with self._db.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE download_tasks
                SET bytes_done = ?,
                    bytes_total = COALESCE(?, bytes_total),
                    updated_at = {_NOW}
                WHERE id = ?
                """,
                (max(bytes_done, 0), bytes_total, task_id),
            )

    async def cancel_active(self, task_id: int) -> DownloadTask | None:
        """Cancel only a non-final task atomically; ``None`` if already final."""

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE download_tasks
                SET status = 'cancelled', updated_at = {_NOW}
                WHERE id = ? AND status IN ('queued', 'downloading', 'paused')
                """,
                (task_id,),
            )
            changed = (cursor.rowcount or 0) == 1
            await cursor.close()
            if not changed:
                return None
            row = await self._fetch_task(conn, "d.id = ?", (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def update_status(
        self, task_id: int, status: str, *, error: str | None = None
    ) -> DownloadTask | None:
        """Set an arbitrary (validated) status; clears ``error`` unless given."""

        checked = validate_status(status)
        async with self._db.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE download_tasks
                SET status = ?, error = ?, updated_at = {_NOW}
                WHERE id = ?
                """,
                (checked, error, task_id),
            )
            row = await self._fetch_task(conn, "d.id = ?", (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def mark_started(self, task_id: int) -> DownloadTask | None:
        """Atomically claim a queued task for one attempt.

        Only a ``queued`` row can be claimed, so a task cancelled between
        enqueue and start is silently skipped instead of being downloaded.
        """

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE download_tasks
                SET status = 'downloading',
                    attempts = attempts + 1,
                    error = NULL,
                    updated_at = {_NOW}
                WHERE id = ? AND status = 'queued'
                """,
                (task_id,),
            )
            claimed = (cursor.rowcount or 0) == 1
            await cursor.close()
            if not claimed:
                return None
            row = await self._fetch_task(conn, "d.id = ?", (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def complete(
        self,
        task_id: int,
        *,
        target_path: str,
        bytes_done: int | None = None,
    ) -> DownloadTask | None:
        """Mark a task completed and remember where the verified file landed."""

        async with self._db.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE download_tasks
                SET status = 'completed',
                    error = NULL,
                    target_path = ?,
                    bytes_done = COALESCE(?, bytes_done),
                    bytes_total = COALESCE(?, bytes_total),
                    updated_at = {_NOW}
                WHERE id = ?
                """,
                (target_path, bytes_done, bytes_done, task_id),
            )
            row = await self._fetch_task(conn, "d.id = ?", (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def fail(self, task_id: int, error: str) -> DownloadTask | None:
        async with self._db.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE download_tasks
                SET status = 'failed', error = ?, updated_at = {_NOW}
                WHERE id = ?
                """,
                (error, task_id),
            )
            row = await self._fetch_task(conn, "d.id = ?", (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def requeue(self, task_id: int, *, error: str | None = None) -> DownloadTask | None:
        """Put a failed/interrupted task back into the queue, keeping attempts."""

        return await self.update_status(task_id, "queued", error=error)

    async def reset(self, task_id: int, *, priority: int | None = None) -> DownloadTask | None:
        """Manual retry: clear attempts/error and queue the task again.

        ``bytes_done`` is preserved so the pipeline can resume the ``.part``
        file with an HTTP Range request instead of starting from zero.
        """

        async with self._db.transaction() as conn:
            if priority is None:
                await conn.execute(
                    f"""
                    UPDATE download_tasks
                    SET status = 'queued', attempts = 0, error = NULL, updated_at = {_NOW}
                    WHERE id = ?
                    """,
                    (task_id,),
                )
            else:
                await conn.execute(
                    f"""
                    UPDATE download_tasks
                    SET status = 'queued', attempts = 0, error = NULL, priority = ?,
                        updated_at = {_NOW}
                    WHERE id = ?
                    """,
                    (priority, task_id),
                )
            row = await self._fetch_task(conn, "d.id = ?", (task_id,))
        return row_to_download_task(row) if row is not None else None

    async def delete(self, task_id: int) -> bool:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM download_tasks WHERE id = ?", (task_id,)
            )
            deleted = (cursor.rowcount or 0) > 0
            await cursor.close()
        return deleted

    # ---------------------------------------------------------------- restart

    async def recover_interrupted(self) -> list[DownloadTask]:
        """Restart-safe recovery: ``downloading`` becomes ``queued`` again.

        Returns every queued task in scheduler order (priority DESC, FIFO), so
        ``DownloadScheduler.start()`` can refill its in-memory queue directly.
        Interrupted attempts are not rolled back: ``attempts`` reflects how many
        attempts were really made before the restart.
        """

        async with self._db.transaction() as conn:
            await conn.execute(
                f"""
                UPDATE download_tasks
                SET status = 'queued', updated_at = {_NOW}
                WHERE status = 'downloading'
                """
            )
            rows = await _fetchall(
                conn,
                f"""
                SELECT {TASK_COLUMNS} {TASK_FROM}
                WHERE d.status = 'queued'
                ORDER BY d.priority DESC, d.id ASC
                """,
            )
        return [row_to_download_task(row) for row in rows]

    async def _fetch_task(
        self, conn: aiosqlite.Connection, where: str, parameters: Sequence[Any]
    ) -> Row | None:
        return await _fetchone(conn, _task_sql(where), parameters)
