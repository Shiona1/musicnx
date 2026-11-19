"""P4 download manager: the ``DownloadManager`` implementation (docs/API.md §8.4).

The manager is the transaction boundary between "queue state" and the rest of
the application:

* it owns the ``DownloadRepository`` and the ``DownloadScheduler``;
* it publishes the documented WebSocket events (``download.queued`` /
  ``download.progress`` / ``download.done`` / ``download.failed`` and
  ``library.updated`` after verification);
* it throttles progress persistence/events to ``progress_hz`` (default 2 Hz);
* it writes ``target_path`` and marks a task ``completed`` when the pipeline
  returns a result, then hands the file to the integrator callbacks.

Callbacks the integrator must wire (all optional — an absent callback is
skipped, never faked):

``register_callback(task, result) -> media_file_id | None``
    Register the downloaded file in ``media_files`` (role ``download``) and
    return its id. This is the only way the manager learns which media row to
    verify; without it the task is completed but the library is not touched.

``verify_callback(media_file_id, result) -> None``
    SHA-256/``verified_at`` bookkeeping (``LibraryRepository.set_verified``
    fits this contract). ``library.updated`` is published only after it
    succeeds, so clients never see an offline copy that was not verified.

``discard_callback(task) -> None``
    Remove the partial ``.part`` file when a task is deleted with
    ``remove=True``. The scheduler/pipeline own temp files, so the manager can
    only delegate this.

Album/playlist resolution is delegated to a ``DownloadTrackResolver`` because
neither ``TrackRepository`` nor the yandex repository exposes the frozen query
signatures. With no resolver configured, ``start_album``/``start_playlist``
return an empty list and log a warning instead of fabricating track ids.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import DownloadTask, DownloadTaskPage
from app.downloads.base import DownloadPipeline, PipelineResult
from app.downloads.repository import DownloadRepository
from app.downloads.scheduler import DownloadScheduler
from app.errors import ConflictError, NotFoundError, UnavailableError
from app.logging_setup import get_logger

logger = get_logger("downloads.manager")

FINAL_STATUSES = frozenset({"completed", "failed"})
RESTARTABLE_STATUSES = frozenset({"failed", "cancelled", "paused"})
DEFAULT_PROGRESS_HZ = 2.0

RegisterCallback = Callable[[DownloadTask, PipelineResult], Awaitable[int | None]]
VerifyCallback = Callable[[int, PipelineResult], Awaitable[None]]
DiscardCallback = Callable[[DownloadTask], Awaitable[None]]


class TrackLookup(Protocol):
    """Minimal track existence check needed before queueing (404 instead of FK)."""

    async def exists(self, track_id: int) -> bool: ...


class DownloadTrackResolver(Protocol):
    """Resolve a collection to internal track ids; ``None`` means "not found"."""

    async def album_track_ids(self, album_id: int) -> list[int] | None: ...

    async def playlist_track_ids(self, playlist_id: str) -> list[int] | None: ...


class DownloadManagerService:
    """Queue orchestration for downloads; implements ``base.DownloadManager``."""

    def __init__(
        self,
        *,
        repository: DownloadRepository,
        scheduler: DownloadScheduler,
        bus: EventBus | None = None,
        tracks: TrackLookup | None = None,
        resolver: DownloadTrackResolver | None = None,
        register_callback: RegisterCallback | None = None,
        verify_callback: VerifyCallback | None = None,
        discard_callback: DiscardCallback | None = None,
        downloads_root: Path | None = None,
        progress_hz: float = DEFAULT_PROGRESS_HZ,
    ) -> None:
        self._repository = repository
        self._scheduler = scheduler
        self._bus = bus
        self._tracks = tracks
        self._resolver = resolver
        self._register_callback = register_callback
        self._verify_callback = verify_callback
        self._discard_callback = discard_callback
        self._downloads_root = downloads_root
        self._progress_interval = 1.0 / progress_hz if progress_hz > 0 else 0.0
        self._progress_at: dict[int, float] = {}
        self._closed = False
        # The manager is the scheduler's only consumer; bind hooks as methods so
        # the two objects stay independent of each other's construction order.
        self._scheduler.bind(
            on_progress=self._on_progress,
            on_completed=self._on_completed,
            on_failed=self._on_failed,
            on_retry=self._on_retry,
        )

    # ------------------------------------------------------------ public API

    async def start(self) -> list[DownloadTask]:
        """Start workers and return the tasks recovered after a restart.

        Called from the application lifespan; every ``start_*`` call also
        ensures the scheduler is running, so a missed lifespan hook degrades to
        a lazy start instead of a dead queue. Idempotent: the second call
        returns an empty list.
        """

        if self._closed:
            raise UnavailableError("download manager is shut down")
        return await self._scheduler.start()

    async def start_track(self, track_id: int, *, priority: int = 0) -> DownloadTask:
        """Queue one track; a duplicate returns the existing task unchanged."""

        await self._ensure_started()
        if self._tracks is not None and not await self._tracks.exists(track_id):
            raise NotFoundError("Track not found", details={"track_id": track_id})

        existing = await self._repository.get_by_track(track_id)
        if existing is None:
            task, created = await self._repository.add(track_id, priority=priority)
        elif existing.status in RESTARTABLE_STATUSES:
            task = await self._repository.reset(existing.id, priority=priority)
            created = True
        else:
            # queued/downloading/completed: already known, do not queue twice.
            return existing
        if task is None:  # pragma: no cover - defensive, row vanished
            raise NotFoundError("Track not found", details={"track_id": track_id})
        if created:
            self._scheduler.enqueue(task)
            await self._publish("download.queued", task)
        return task

    async def start_album(self, album_id: int, *, priority: int = 0) -> list[DownloadTask]:
        await self._ensure_started()
        track_ids = await self._resolve_album(album_id)
        return await self._start_many(track_ids, priority=priority)

    async def start_playlist(
        self, playlist_id: str, *, priority: int = 0
    ) -> list[DownloadTask]:
        await self._ensure_started()
        track_ids = await self._resolve_playlist(playlist_id)
        return await self._start_many(track_ids, priority=priority)

    async def list(self, *, status: str | None = None) -> DownloadTaskPage:
        return await self._repository.list(status)

    async def cancel(
        self, task_id: int, *, remove: bool = False
    ) -> DownloadTask | None:
        """Soft cancel (``cancelled``, partial kept) or delete the record.

        With ``remove=True`` the running job is cancelled, the optional discard
        callback removes the partial file and the row is deleted; ``None`` is
        returned. A missing task always raises ``NotFoundError`` so the API can
        answer 404 for both variants.
        """

        task = await self._repository.get(task_id)
        if task is None:
            raise NotFoundError(
                "Download task not found", details={"id": task_id}
            )
        await self._scheduler.cancel(task_id)
        self._progress_at.pop(task_id, None)
        if remove:
            if self._discard_callback is not None:
                try:
                    await self._discard_callback(task)
                except Exception:  # pragma: no cover - cleanup must not break DELETE
                    logger.exception("cannot discard partial file of download %s", task_id)
            await self._repository.delete(task_id)
            return None
        if task.status in FINAL_STATUSES:
            raise ConflictError(
                "a finished download cannot be cancelled",
                details={"id": task_id, "status": task.status},
            )
        if task.status == "cancelled":
            return task
        cancelled = await self._repository.cancel_active(task_id)
        if cancelled is not None:
            return cancelled
        # A completion/failure won the race after scheduler.cancel.
        current = await self._repository.get(task_id)
        if current is not None and current.status in FINAL_STATUSES:
            raise ConflictError(
                "a finished download cannot be cancelled",
                details={"id": task_id, "status": current.status},
            )
        return current

    async def retry(self, task_id: int) -> DownloadTask:
        """Reset attempts/error and queue the task again (docs/API.md §8.2)."""

        await self._ensure_started()
        task = await self._repository.get(task_id)
        if task is None:
            raise NotFoundError(
                "Download task not found", details={"id": task_id}
            )
        if task.status == "downloading":
            raise ConflictError(
                "download is already running",
                details={"id": task_id, "status": task.status},
            )
        if task.status == "completed":
            raise ConflictError(
                "download is already completed",
                details={"id": task_id, "status": task.status},
            )
        if task.status == "queued":
            return task
        reset = await self._repository.reset(task_id)
        if reset is None:  # pragma: no cover - defensive
            raise NotFoundError("Download task not found", details={"id": task_id})
        self._scheduler.enqueue(reset)
        await self._publish("download.queued", reset)
        return reset

    async def shutdown(self) -> None:
        """Stop workers; interrupted tasks are re-queued by the scheduler."""

        if self._closed:
            return
        self._closed = True
        await self._scheduler.stop()
        self._progress_at.clear()

    # ------------------------------------------------------------- resolution

    async def _resolve_album(self, album_id: int) -> list[int]:
        if self._resolver is None:
            logger.warning(
                "downloads: no album resolver configured; album %s ignored", album_id
            )
            return []
        track_ids = await self._resolver.album_track_ids(album_id)
        if track_ids is None:
            raise NotFoundError("Album not found", details={"album_id": album_id})
        return list(dict.fromkeys(track_ids))

    async def _resolve_playlist(self, playlist_id: str) -> list[int]:
        if self._resolver is None:
            logger.warning(
                "downloads: no playlist resolver configured; playlist %s ignored",
                playlist_id,
            )
            return []
        track_ids = await self._resolver.playlist_track_ids(playlist_id)
        if track_ids is None:
            raise NotFoundError(
                "Playlist not found", details={"playlist_id": playlist_id}
            )
        return list(dict.fromkeys(track_ids))

    async def _start_many(
        self, track_ids: list[int], *, priority: int
    ) -> list[DownloadTask]:
        tasks: list[DownloadTask] = []
        for track_id in track_ids:
            tasks.append(await self.start_track(track_id, priority=priority))
        return tasks

    # ---------------------------------------------------------- scheduler hooks

    async def _on_progress(
        self, task_id: int, bytes_done: int, bytes_total: int | None
    ) -> None:
        """Throttle progress writes and events to ``progress_hz`` (default 2 Hz)."""

        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._progress_at.get(task_id, 0.0) < self._progress_interval:
            return
        self._progress_at[task_id] = now
        try:
            await self._repository.update_progress(task_id, bytes_done, bytes_total)
            task = await self._repository.get(task_id)
        except Exception:  # progress is advisory: never fail a download over it
            logger.exception("cannot persist progress for download %s", task_id)
            return
        if task is not None:
            await self._publish("download.progress", task)

    async def _on_completed(self, task: DownloadTask, result: PipelineResult) -> None:
        completed = await self._repository.complete(
            task.id,
            target_path=self._target_path(result),
            bytes_done=result.bytes_written,
        )
        self._progress_at.pop(task.id, None)
        if completed is None:  # pragma: no cover - row deleted while downloading
            return
        verified = False
        try:
            media_file_id: int | None = None
            if self._register_callback is not None:
                media_file_id = await self._register_callback(completed, result)
            if media_file_id is not None and self._verify_callback is not None:
                await self._verify_callback(media_file_id, result)
                verified = True
        except Exception as exc:
            # Bytes are on disk but the library cannot use them: be honest and
            # mark the task failed instead of advertising a broken offline copy.
            logger.exception("download %s post-processing failed", task.id)
            failed = await self._repository.fail(
                task.id, f"post-processing failed: {exc}"
            )
            if failed is not None:
                await self._publish("download.failed", failed)
            return
        await self._publish("download.done", completed)
        if verified:
            await self._publish_library_updated()

    async def _on_failed(self, task: DownloadTask) -> None:
        self._progress_at.pop(task.id, None)
        await self._publish("download.failed", task)

    async def _on_retry(self, task: DownloadTask, delay_s: float) -> None:
        # A retry is "возобновлена" per docs/API.md §7: re-announce the queued task.
        self._progress_at.pop(task.id, None)
        await self._publish("download.queued", task)

    # ---------------------------------------------------------------- helpers

    def _target_path(self, result: PipelineResult) -> str:
        """Persist the API's relative ``target_path`` when the root is known.

        Pipelines return absolute paths (that is what they wrote); docs/API.md
        §8.1 shows ``target_path`` relative to the downloads root. With
        ``downloads_root`` wired the manager normalises to a POSIX relative
        path, otherwise it stores the pipeline value verbatim.
        """

        if self._downloads_root is None:
            return result.path
        try:
            resolved = Path(result.path).resolve()
            root = self._downloads_root.resolve()
        except OSError:  # pragma: no cover - unusual filesystem error
            return result.path
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
        return result.path

    async def _ensure_started(self) -> None:
        if self._closed:
            raise UnavailableError("download manager is shut down")
        await self._scheduler.start()

    async def _publish(self, event_type: str, task: DownloadTask) -> None:
        if self._bus is None:
            return
        await self._bus.publish(event_type, {"task": task.model_dump()})

    async def _publish_library_updated(self) -> None:
        if self._bus is None:
            return
        # Same payload shape as the import event; clients reload their shelves.
        await self._bus.publish("library.updated", {"tracks": 1, "missing": 0})


def create_download_manager(
    *,
    repository: DownloadRepository,
    pipeline: DownloadPipeline,
    settings: Settings,
    bus: EventBus | None = None,
    tracks: TrackLookup | None = None,
    resolver: DownloadTrackResolver | None = None,
    register_callback: RegisterCallback | None = None,
    verify_callback: VerifyCallback | None = None,
    discard_callback: DiscardCallback | None = None,
    downloads_root: Path | None = None,
    progress_hz: float = DEFAULT_PROGRESS_HZ,
) -> DownloadManagerService:
    """Wire the scheduler from ``settings.downloads`` and the manager around it."""

    scheduler = DownloadScheduler.from_settings(repository, pipeline, settings)
    return DownloadManagerService(
        repository=repository,
        scheduler=scheduler,
        bus=bus,
        tracks=tracks,
        resolver=resolver,
        register_callback=register_callback,
        verify_callback=verify_callback,
        discard_callback=discard_callback,
        downloads_root=downloads_root or settings.paths.downloads,
        progress_hz=progress_hz,
    )
