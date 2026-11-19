"""SQLite access for ``sync_runs`` and the minimal playlist lookup sync needs.

Only SQL lives here; the service layer never builds queries (the same boundary
as :mod:`app.integrations.yandex_music.repository`). The schema is frozen by
``0001_init.sql``: this module never creates or alters tables (AGENTS.md §7).

Counts/errors written by :meth:`SyncRepository.finish_run` are supplied by the
service, which is responsible for redacting upstream error text before it is
persisted and broadcast (AGENTS.md §8).
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.models import SyncRun, SyncStatusValue
from app.core.timeutils import utc_now_iso
from app.database.connection import Database, Row

#: Count columns of ``sync_runs`` in schema order (docs/API.md §10).
COUNT_FIELDS: tuple[str, ...] = (
    "playlists_added",
    "playlists_updated",
    "playlists_removed",
    "tracks_added",
    "tracks_updated",
    "tracks_removed",
)

_SYNC_STATUSES: frozenset[str] = frozenset(
    {"running", "success", "partial", "failed", "cancelled"}
)
_TRUE_STATUSES: frozenset[str] = frozenset({"success", "partial", "failed", "cancelled"})

#: History is bounded; the API validates the same range. Keeps one query cheap.
MAX_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20


def row_to_sync_run(row: Row) -> SyncRun:
    """Convert one ``sync_runs`` row into the frozen ``SyncRun`` DTO."""

    return SyncRun(
        id=int(row["id"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        playlists_added=int(row["playlists_added"]),
        playlists_updated=int(row["playlists_updated"]),
        playlists_removed=int(row["playlists_removed"]),
        tracks_added=int(row["tracks_added"]),
        tracks_updated=int(row["tracks_updated"]),
        tracks_removed=int(row["tracks_removed"]),
        error=row["error"],
    )


class SyncRepository:
    """Run history plus the one catalog lookup the sync diff needs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------------------------------------------------------------- run rows

    async def start_run(self) -> SyncRun:
        """Insert a ``running`` row and return it (single-flight is the service)."""

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')",
                (utc_now_iso(),),
            )
            run_id = int(cursor.lastrowid or 0)
            await cursor.close()
        run = await self.get(run_id)
        if run is None:  # pragma: no cover - defensive
            raise RuntimeError("sync_runs insert did not produce a row")
        return run

    async def finish_run(
        self,
        run_id: int,
        status: SyncStatusValue,
        counts: Mapping[str, int] | None = None,
        error: str | None = None,
    ) -> SyncRun | None:
        """Close a run with its final status/counts; ``None`` if it disappeared.

        Missing count keys keep their previous value, so a cancelled run can
        persist the partial progress it already accumulated.
        """

        if status not in _TRUE_STATUSES:
            raise ValueError(f"invalid final sync status: {status!r}")
        values = {
            field: int(counts[field]) if counts and field in counts else None
            for field in COUNT_FIELDS
        }
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, status = ?,
                    playlists_added = COALESCE(?, playlists_added),
                    playlists_updated = COALESCE(?, playlists_updated),
                    playlists_removed = COALESCE(?, playlists_removed),
                    tracks_added = COALESCE(?, tracks_added),
                    tracks_updated = COALESCE(?, tracks_updated),
                    tracks_removed = COALESCE(?, tracks_removed),
                    error = ?
                WHERE id = ?
                """,
                (
                    utc_now_iso(),
                    status,
                    values["playlists_added"],
                    values["playlists_updated"],
                    values["playlists_removed"],
                    values["tracks_added"],
                    values["tracks_updated"],
                    values["tracks_removed"],
                    error,
                    run_id,
                ),
            )
            await cursor.close()
        return await self.get(run_id)

    async def latest(self) -> SyncRun | None:
        """Newest run by insertion id (``started_at`` can tie within a second)."""

        row = await self._db.fetchone(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
        )
        return row_to_sync_run(row) if row is not None else None

    async def latest_finished(self) -> SyncRun | None:
        """Newest *completed* run, ignoring one that is still ``running``.

        ``SyncStatus.last`` is the last attempt the UI can report on; the active
        run is shown through ``running``/``progress`` and appears in history
        (docs/API.md §10, docs/SYNC.md §3).
        """

        row = await self._db.fetchone(
            "SELECT * FROM sync_runs WHERE status <> 'running' "
            "ORDER BY id DESC LIMIT 1"
        )
        return row_to_sync_run(row) if row is not None else None

    async def history(
        self, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[SyncRun]:
        """Runs newest first, capped at :data:`MAX_HISTORY_LIMIT`."""

        bounded = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        rows = await self._db.fetchall(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (bounded,)
        )
        return [row_to_sync_run(row) for row in rows]

    async def get(self, run_id: int) -> SyncRun | None:
        row = await self._db.fetchone(
            "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
        )
        return row_to_sync_run(row) if row is not None else None

    async def count(self) -> int:
        return int(await self._db.scalar("SELECT COUNT(*) FROM sync_runs") or 0)

    async def close_stale_running(self, error: str) -> int:
        """Mark rows left ``running`` by a crashed process as ``failed``.

        A new process owns no in-memory task, so such a row can never finish;
        leaving it ``running`` would make the UI show a phantom sync forever.
        """

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE sync_runs SET status = 'failed', finished_at = ?, error = ? "
                "WHERE status = 'running'",
                (utc_now_iso(), error),
            )
            changed = int(cursor.rowcount or 0)
            await cursor.close()
        return changed

    # ------------------------------------------------------------- catalog read

    async def playlist_ref(self, external_id: str) -> int | None:
        """Internal id of a user playlist by its Yandex ``<uid>:<kind>`` id.

        Sync needs the local id *before* calling the adapter, because adapters
        upsert membership themselves and the pre-run composition is the diff
        baseline. Only ``kind='user'`` rows are considered (likes/wave/local are
        system playlists and are not part of catalog sync).
        """

        row = await self._db.fetchone(
            "SELECT id FROM playlists WHERE external_id = ? AND kind = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (external_id,),
        )
        return int(row["id"]) if row is not None else None


__all__ = [
    "COUNT_FIELDS",
    "DEFAULT_HISTORY_LIMIT",
    "MAX_HISTORY_LIMIT",
    "SyncRepository",
    "row_to_sync_run",
]
