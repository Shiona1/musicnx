"""Single-flight catalog sync engine (P6, docs/API.md §10).

The service is the only writer of ``sync_runs`` and the only caller of the
frozen :class:`~app.integrations.yandex_music.base.YandexAdapter` playlist
methods for synchronization. It owns:

* **single flight** — at most one background ``asyncio.Task`` per process;
  a second ``start()`` raises ``409 sync_running``;
* **composition diff** — every remote playlist is compared with the local
  ``playlist_tracks`` composition, then replaced atomically so removals and
  reorders are mirrored (adapters themselves only upsert additively);
* **cooperative cancel** — the flag is checked between playlists, so the
  current playlist always finishes and no half-written composition is left;
* **bounded backoff** — only transient failures (429/5xx/timeouts, mapped by
  the adapter to ``UnavailableError``) are retried; a broken playlist is
  skipped and the run becomes ``partial``;
* **hourly scheduler** — only when ``settings.sync.enabled``; the first run is
  one interval after startup, never immediately (docs/API.md §10).

Nothing here logs or broadcasts tokens. Playlist titles and track metadata are
never logged; persisted/broadcast errors pass through URL stripping and the
logging redaction filter (AGENTS.md §8). All I/O is awaited, so the event loop
is never blocked (AGENTS.md §6).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import TypeVar

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import (
    SyncHistory,
    SyncRun,
    SyncStatus,
    SyncStatusValue,
    YandexPlaylist,
)
from app.core.timeutils import to_iso, utc_now
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.errors import ConflictError, UnavailableError
from app.integrations.yandex_music.base import YandexAdapter
from app.integrations.yandex_music.mapper import NormalizedPlaylist
from app.integrations.yandex_music.repository import YandexRepository
from app.logging_setup import get_logger, redact
from app.sync.repository import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    SyncRepository,
)

logger = get_logger("sync.service")

T = TypeVar("T")

#: One adapter page; the frozen contract pages by ``limit``/``offset``.
DEFAULT_PLAYLIST_PAGE_SIZE = 100
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S: tuple[float, ...] = (2.0, 8.0)
MAX_ERROR_LENGTH = 500
#: Defensive bound for an adapter that ignores ``offset`` (never in tests).
MAX_PLAYLIST_PAGES = 100
#: How long graceful shutdown waits for a cooperative cancel before forcing it.
SHUTDOWN_WAIT_S = 5.0

#: Known trigger sources; anything else is treated as a manual run.
_SYNC_REASONS = frozenset({"manual", "hourly"})

_STATUS_EVENTS: dict[str, str] = {
    "success": "sync.completed",
    "partial": "sync.completed",
    "failed": "sync.failed",
    "cancelled": "sync.cancelled",
}

_URL_PATTERN = re.compile(r"https?://\S+")


def _safe_error(exc: BaseException) -> str:
    """Short, redacted failure text safe to persist and broadcast.

    Upstream exceptions may embed signed URLs or query strings; those are
    replaced before the message reaches SQLite, logs or the WebSocket.
    """

    message = " ".join(str(exc).split()) or type(exc).__name__
    message = _URL_PATTERN.sub("[url]", message)
    message = redact(message)
    return message[:MAX_ERROR_LENGTH]


def _short_id(value: str | None) -> str:
    """Bound a remote playlist id used in logs/errors (no titles or tokens)."""

    return (value or "")[:64]


def _is_retryable(exc: BaseException) -> bool:
    """Classify adapter failures without importing the Yandex library.

    The real adapter maps transient upstream errors to ``UnavailableError``;
    mocks/tests may instead carry an HTTP ``status_code``/``status``. Permanent
    problems (auth, not found, schema contract) are never retried.
    """

    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, UnavailableError):
        return True
    for attribute in ("status_code", "status"):
        status = getattr(exc, attribute, None)
        if isinstance(status, int):
            return status in {408, 425, 429} or 500 <= status < 600
    return isinstance(exc, TimeoutError | OSError)


@dataclass
class _Counts:
    """Mutable accumulator flushed into ``sync_runs`` exactly once per run."""

    playlists_added: int = 0
    playlists_updated: int = 0
    playlists_removed: int = 0
    tracks_added: int = 0
    tracks_updated: int = 0
    tracks_removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "playlists_added": self.playlists_added,
            "playlists_updated": self.playlists_updated,
            "playlists_removed": self.playlists_removed,
            "tracks_added": self.tracks_added,
            "tracks_updated": self.tracks_updated,
            "tracks_removed": self.tracks_removed,
        }


def _diff(before: Sequence[int], after: Sequence[int]) -> tuple[int, int, int]:
    """Return ``(added, updated, removed)`` track counts for one playlist.

    ``updated`` counts common tracks whose position changed; an identical rerun
    therefore reports zeros (the data was already in sync).
    """

    before_set = set(before)
    after_set = set(after)
    added = sum(1 for track_id in after if track_id not in before_set)
    removed = sum(1 for track_id in before if track_id not in after_set)
    before_common = [track_id for track_id in before if track_id in after_set]
    after_common = [track_id for track_id in after if track_id in before_set]
    updated = sum(
        1
        for previous, current in zip(before_common, after_common, strict=False)
        if previous != current
    )
    return added, updated, removed


def _normalized_playlist(
    playlist: YandexPlaylist, external_id: str, track_count: int
) -> NormalizedPlaylist:
    """Build the repository input from the frozen playlist DTO.

    ``YandexPlaylist`` has no ``revision``; the repository keeps the stored
    revision via ``COALESCE`` and the diff falls back to composition (which the
    adapter contract always supports).
    """

    uid, _, kind_suffix = external_id.partition(":")
    kind = int(kind_suffix) if kind_suffix.isdigit() else int(playlist.kind or 0)
    return NormalizedPlaylist(
        external_id=external_id,
        uid=uid or playlist.owner or "",
        kind=kind,
        title=playlist.title or external_id,
        track_count=max(int(track_count), 0),
        cover_url=playlist.cover_url,
        owner=playlist.owner,
        revision=None,
    )


class SyncService:
    """Single-flight Yandex catalog synchronization with run history."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        db: Database,
        yandex_adapter: YandexAdapter,
        yandex_repository: YandexRepository,
        tracks: TrackRepository | None = None,
        *,
        repository: SyncRepository | None = None,
        page_size: int = DEFAULT_PLAYLIST_PAGE_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_s: Sequence[float] = DEFAULT_BACKOFF_S,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._db = db
        self._adapter = yandex_adapter
        self._yandex_repository = yandex_repository
        # Kept for future metadata refreshes and for symmetry with other
        # services; sync itself must not duplicate adapter-owned upserts.
        self._tracks = tracks
        self._repository = repository or SyncRepository(db)
        self._page_size = max(1, int(page_size))
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_s = tuple(max(0.0, float(delay)) for delay in backoff_s)

        self._start_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._run_id: int | None = None
        self._progress: str | None = None
        self._next_run_at: datetime | None = None
        self._cancel_requested = False
        self._closed = False

    # ------------------------------------------------------------------ public

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def status(self) -> SyncStatus:
        """Current run state plus the latest completed run (read-only)."""

        return SyncStatus(
            running=self.is_running,
            last=await self._repository.latest_finished(),
            next_run_at=to_iso(self._next_run_at) if self._next_run_at else None,
            interval_s=int(self._settings.sync.interval_s),
            progress=self._progress,
        )

    async def history(self, limit: int = DEFAULT_HISTORY_LIMIT) -> SyncHistory:
        """Runs newest first; ``total`` is all stored runs, not just this page."""

        bounded = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        items = await self._repository.history(bounded)
        return SyncHistory(items=items, total=await self._repository.count())

    async def start(self, *, reason: str) -> SyncStatus:
        """Start one background run; ``409 sync_running`` when already active."""

        normalized_reason = reason if reason in _SYNC_REASONS else "manual"
        async with self._start_lock:
            if self.is_running:
                raise ConflictError(
                    "a sync run is already active", code="sync_running"
                )
            if self._closed:
                raise UnavailableError(
                    "sync service is shutting down", code="sync_unavailable"
                )
            # Read the previous completed run before scheduling the task: after
            # ``create_task`` this coroutine no longer awaits, so the 202 body is
            # a stable snapshot (running=True, last=predecessor).
            last = await self._repository.latest_finished()
            run = await self._repository.start_run()
            self._cancel_requested = False
            self._run_id = run.id
            self._progress = "starting"
            self._task = asyncio.create_task(
                self._execute(run, normalized_reason),
                name=f"sync-run-{run.id}",
            )
            logger.info(
                "sync run started: run_id=%s reason=%s", run.id, normalized_reason
            )
        return SyncStatus(
            running=True,
            last=last,
            next_run_at=to_iso(self._next_run_at) if self._next_run_at else None,
            interval_s=int(self._settings.sync.interval_s),
            progress=self._progress,
        )

    async def cancel(self) -> SyncRun:
        """Request cancellation between playlists; ``409 sync_not_running``."""

        if not self.is_running or self._run_id is None:
            raise ConflictError("no sync run is active", code="sync_not_running")
        run = await self._repository.get(self._run_id)
        if run is None or run.status != "running":
            raise ConflictError("no sync run is active", code="sync_not_running")
        self._cancel_requested = True
        logger.info("sync cancel requested: run_id=%s", run.id)
        return run

    async def wait_idle(self) -> None:
        """Await the active run (tests/administrative); safe when idle."""

        task = self._task
        if task is not None and not task.done():
            await asyncio.shield(task)

    async def startup(self) -> None:
        """Recover stale rows and start the hourly loop when enabled."""

        self._closed = False
        changed = await self._repository.close_stale_running("interrupted by restart")
        if changed:
            logger.warning("sync: %s stale run(s) marked failed", changed)
        await self.start_scheduler()

    async def start_scheduler(self) -> None:
        """Start the hourly loop if ``sync.enabled``; the first run is delayed."""

        if not self._settings.sync.enabled:
            logger.info("sync scheduler disabled: sync.enabled=false")
            return
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._closed = False
        self._wake.clear()
        # Publish the first deadline synchronously so ``status()`` is honest
        # immediately after startup (the loop recomputes it each pass).
        self._next_run_at = utc_now() + timedelta(
            seconds=max(float(self._settings.sync.interval_s), 0.01)
        )
        self._loop_task = asyncio.create_task(
            self._hourly_loop(), name="sync-scheduler"
        )

    async def stop(self) -> None:
        """Stop the scheduler and let an active run finish or cancel gracefully."""

        self._closed = True
        self._wake.set()
        loop_task = self._loop_task
        self._loop_task = None
        if loop_task is not None and not loop_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(loop_task), timeout=SHUTDOWN_WAIT_S
                )
            except TimeoutError:
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)
            except asyncio.CancelledError:
                loop_task.cancel()
                raise
        self._next_run_at = None

        task = self._task
        if task is not None and not task.done():
            self._cancel_requested = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=SHUTDOWN_WAIT_S)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                task.cancel()
                raise
        logger.info("sync service stopped")

    async def shutdown(self) -> None:
        """Alias for :meth:`stop` (lifespan shutdown hook)."""

        await self.stop()

    # --------------------------------------------------------------- scheduler

    async def _hourly_loop(self) -> None:
        interval = max(float(self._settings.sync.interval_s), 0.01)
        logger.info("sync scheduler started: interval_s=%s", interval)
        try:
            while not self._closed:
                self._next_run_at = utc_now() + timedelta(seconds=interval)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=interval)
                except TimeoutError:
                    pass
                else:  # woken by stop()
                    break
                if self._closed:
                    break
                # Keep a future ``next_run_at`` visible while the run is active.
                self._next_run_at = utc_now() + timedelta(seconds=interval)
                try:
                    await self.start(reason="hourly")
                except ConflictError:
                    logger.info(
                        "sync: hourly run skipped; another run is already active"
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception("sync: hourly run could not start")
                task = self._task
                if task is not None:
                    # Never overlap runs: wait for this one before sleeping again.
                    await asyncio.shield(task)
        finally:
            self._next_run_at = None
            logger.info("sync scheduler stopped")

    # ------------------------------------------------------------ run pipeline

    async def _execute(self, run: SyncRun, reason: str) -> None:
        counts = _Counts()
        try:
            await self._publish(
                "sync.started",
                {"run": run.model_dump(mode="json"), "reason": reason},
            )
            status, error = await self._run_sync(run, counts)
        except asyncio.CancelledError:
            await self._finalize(run, "cancelled", counts, None)
            raise
        except Exception as exc:  # any unexpected failure still closes the run
            await self._finalize(run, "failed", counts, _safe_error(exc))
        else:
            await self._finalize(run, status, counts, error)
        finally:
            self._run_id = None
            self._progress = None
            self._task = None

    async def _run_sync(
        self, run: SyncRun, counts: _Counts
    ) -> tuple[SyncStatusValue, str | None]:
        if self._cancel_requested:
            return "cancelled", None

        await self._publish_progress(0, 0)
        page = await self._retry("playlists", self._adapter.playlists)
        playlists = list(page.items)
        total = len(playlists)
        await self._publish_progress(0, total)

        errors: list[str] = []
        succeeded = 0
        for index, playlist in enumerate(playlists, start=1):
            if self._cancel_requested:
                return "cancelled", None
            self._progress = f"playlists {index}/{total}"
            await self._publish_progress(index, total)
            try:
                await self._sync_playlist(playlist, counts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = _safe_error(exc)
                errors.append(f"{_short_id(playlist.id)}: {message}")
                logger.warning(
                    "sync: playlist failed: playlist=%s error=%s",
                    _short_id(playlist.id),
                    message,
                )
            else:
                succeeded += 1

        if self._cancel_requested:
            return "cancelled", None
        if errors:
            status: SyncStatusValue = "partial" if succeeded else "failed"
            return status, _join_errors(errors)
        logger.info("sync run body finished: run_id=%s playlists=%s", run.id, total)
        return "success", None

    async def _sync_playlist(
        self, playlist: YandexPlaylist, counts: _Counts
    ) -> None:
        external_id = playlist.id
        local_id = await self._repository.playlist_ref(external_id)
        before = (
            await self._yandex_repository.playlist_track_ids(local_id)
            if local_id is not None
            else []
        )

        remote_playlist, remote_ids, total = await self._fetch_playlist(playlist)

        # The adapter already upserted tracks; this call guarantees the playlist
        # row exists (mock adapters do not persist playlists on their own).
        normalized = _normalized_playlist(remote_playlist, external_id, total)
        playlist_id = await self._yandex_repository.upsert_playlist(normalized)
        # Replace, not merge: composition and order become exactly the remote
        # one. Skip the write when nothing moved so an idempotent rerun leaves
        # ``playlist_tracks`` untouched (AGENTS acceptance for P6).
        if remote_ids != before:
            await self._yandex_repository.replace_playlist_tracks(
                playlist_id, remote_ids
            )

        added, updated, removed = _diff(before, remote_ids)
        counts.tracks_added += added
        counts.tracks_updated += updated
        counts.tracks_removed += removed
        if local_id is None:
            counts.playlists_added += 1
        elif added or updated or removed:
            counts.playlists_updated += 1
        logger.info(
            "sync: playlist synced: playlist=%s added=%d updated=%d removed=%d",
            _short_id(external_id),
            added,
            updated,
            removed,
        )

    async def _fetch_playlist(
        self, playlist: YandexPlaylist
    ) -> tuple[YandexPlaylist, list[int], int]:
        """Page through one playlist; returns DTO, internal track ids and total."""

        external_id = playlist.id
        remote_ids: list[int] = []
        remote_playlist = playlist
        total = max(int(playlist.track_count or 0), 0)
        offset = 0
        previous_page: tuple[int, ...] = ()
        pages = 0

        while True:
            fetcher = partial(
                self._adapter.playlist,
                external_id,
                limit=self._page_size,
                offset=offset,
            )
            remote_playlist, page = await self._retry("playlist", fetcher)
            items = list(page.items)
            page_ids = tuple(int(track.id) for track in items)
            if page_ids and page_ids == previous_page:
                # An adapter ignoring ``offset`` would otherwise loop forever.
                break
            remote_ids.extend(page_ids)
            previous_page = page_ids
            offset += len(items)
            total = max(total, int(page.total or 0))
            pages += 1
            if not items or offset >= total or pages >= MAX_PLAYLIST_PAGES:
                break

        return remote_playlist, remote_ids, total

    # ----------------------------------------------------------------- helpers

    async def _retry(
        self, operation: str, factory: Callable[[], Awaitable[T]]
    ) -> T:
        attempt = 0
        while True:
            try:
                return await factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                if attempt >= self._max_attempts or not _is_retryable(exc):
                    raise
                delay = self._backoff_delay(attempt - 1)
                logger.info(
                    "sync retry: op=%s attempt=%d delay=%.1fs error=%s",
                    operation,
                    attempt,
                    delay,
                    _safe_error(exc),
                )
                if delay > 0:
                    await asyncio.sleep(delay)

    def _backoff_delay(self, index: int) -> float:
        if not self._backoff_s:
            return 0.0
        return self._backoff_s[min(max(index, 0), len(self._backoff_s) - 1)]

    async def _publish_progress(self, current: int, total: int) -> None:
        self._progress = f"playlists {current}/{total}" if total else "playlists"
        await self._publish(
            "sync.progress",
            {"phase": "playlists", "current": current, "total": total},
        )

    async def _publish(self, event_type: str, payload: dict[str, object]) -> None:
        try:
            await self._bus.publish(event_type, payload)
        except Exception:  # pragma: no cover - a bus failure must not fail a run
            logger.warning("sync: event publish failed: %s", event_type)

    async def _finalize(
        self,
        run: SyncRun,
        status: SyncStatusValue,
        counts: _Counts,
        error: str | None,
    ) -> None:
        finished = await self._repository.finish_run(
            run.id, status, counts.as_dict(), error
        )
        if finished is None:  # pragma: no cover - row deleted mid-run
            logger.warning("sync: run row disappeared: run_id=%s", run.id)
            return
        await self._publish(
            _STATUS_EVENTS[status], {"run": finished.model_dump(mode="json")}
        )
        logger.info(
            "sync run finished: run_id=%s status=%s counts=%s",
            finished.id,
            status,
            counts.as_dict(),
        )


def _join_errors(errors: Sequence[str]) -> str:
    return ("; ".join(errors))[:MAX_ERROR_LENGTH]


__all__ = [
    "DEFAULT_BACKOFF_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PLAYLIST_PAGE_SIZE",
    "MAX_ERROR_LENGTH",
    "SyncService",
]
