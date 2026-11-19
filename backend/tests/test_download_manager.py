"""P4 downloads: repository, scheduler, manager and API contract tests.

Mock-first, as required by AGENTS.md §4: in-memory/mock repository, pipeline and
event bus, temp SQLite for the real repository. No network, no Raspberry Pi, no
real audio and no sleep longer than a few milliseconds.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import app.api.deps as deps
import pytest
from app.api.routes import downloads as downloads_api
from app.config import DownloadsSettings
from app.core.models import DownloadTask, DownloadTaskPage
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.downloads.base import PipelineResult
from app.downloads.manager import DownloadManagerService
from app.downloads.repository import DownloadRepository
from app.downloads.scheduler import (
    DownloadScheduler,
    PermanentDownloadError,
    RetryableDownloadError,
    is_retryable_error,
)
from app.errors import ConflictError, NotFoundError
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

NOW = "2026-11-14T12:00:00.000Z"


# --------------------------------------------------------------------- helpers


def make_result(track_id: int, *, path: str | None = None) -> PipelineResult:
    return PipelineResult(
        path=path or f"Artist/Album (2024)/Disc 1/01 - Track {track_id}.flac",
        format="flac",
        quality="flac",
        checksum="a" * 64,
        bytes_written=123,
        title=f"Track {track_id}",
        artists=("Artist",),
        album="Album",
        year=2024,
    )


class FakeBus:
    """Records ``EventBus.publish`` calls (same signature as the real bus)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def names(self) -> list[str]:
        return [name for name, _payload in self.events]

    def count(self, name: str) -> int:
        return sum(1 for event_name, _payload in self.events if event_name == name)


class FakeDownloadRepository:
    """In-memory stand-in with the ``DownloadRepository`` scheduler contract."""

    def __init__(self) -> None:
        self.tasks: dict[int, DownloadTask] = {}
        self._next_id = 1

    def seed(
        self,
        track_id: int,
        *,
        status: str = "queued",
        priority: int = 0,
        attempts: int = 0,
        bytes_done: int = 0,
    ) -> DownloadTask:
        task = DownloadTask(
            id=self._next_id,
            track_id=track_id,
            title=f"Track {track_id}",
            status=status,
            priority=priority,
            attempts=attempts,
            bytes_done=bytes_done,
            created_at=NOW,
            updated_at=NOW,
        )
        self.tasks[task.id] = task
        self._next_id += 1
        return task

    def _update(self, task_id: int, **changes: object) -> DownloadTask | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        updated = task.model_copy(update=changes)
        self.tasks[task_id] = updated
        return updated

    async def get(self, task_id: int) -> DownloadTask | None:
        return self.tasks.get(task_id)

    async def get_by_track(self, track_id: int) -> DownloadTask | None:
        for task in self.tasks.values():
            if task.track_id == track_id:
                return task
        return None

    async def add(self, track_id: int, *, priority: int = 0) -> tuple[DownloadTask, bool]:
        existing = await self.get_by_track(track_id)
        if existing is not None:
            return existing, False
        return self.seed(track_id, priority=priority), True

    async def list(self, status: str | None = None) -> DownloadTaskPage:
        items = [
            task
            for task in self.tasks.values()
            if status is None or task.status == status
        ]
        items.sort(key=lambda task: task.id, reverse=True)
        return DownloadTaskPage(items=items, total=len(items))

    async def update_progress(
        self, task_id: int, bytes_done: int, bytes_total: int | None = None
    ) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        self._update(
            task_id,
            bytes_done=bytes_done,
            bytes_total=bytes_total if bytes_total is not None else task.bytes_total,
        )

    async def update_status(
        self, task_id: int, status: str, *, error: str | None = None
    ) -> DownloadTask | None:
        return self._update(task_id, status=status, error=error)

    async def mark_started(self, task_id: int) -> DownloadTask | None:
        task = self.tasks.get(task_id)
        if task is None or task.status != "queued":
            return None
        return self._update(
            task_id, status="downloading", attempts=task.attempts + 1, error=None
        )

    async def complete(
        self, task_id: int, *, target_path: str, bytes_done: int | None = None
    ) -> DownloadTask | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        return self._update(
            task_id,
            status="completed",
            error=None,
            target_path=target_path,
            bytes_done=bytes_done if bytes_done is not None else task.bytes_done,
        )

    async def fail(self, task_id: int, error: str) -> DownloadTask | None:
        return self._update(task_id, status="failed", error=error)

    async def requeue(
        self, task_id: int, *, error: str | None = None
    ) -> DownloadTask | None:
        return self._update(task_id, status="queued", error=error)

    async def reset(self, task_id: int, *, priority: int | None = None) -> DownloadTask | None:
        changes: dict[str, object] = {
            "status": "queued",
            "attempts": 0,
            "error": None,
        }
        if priority is not None:
            changes["priority"] = priority
        return self._update(task_id, **changes)

    async def delete(self, task_id: int) -> bool:
        return self.tasks.pop(task_id, None) is not None

    async def recover_interrupted(self) -> list[DownloadTask]:
        for task_id, task in list(self.tasks.items()):
            if task.status == "downloading":
                self._update(task_id, status="queued")
        queued = [task for task in self.tasks.values() if task.status == "queued"]
        queued.sort(key=lambda task: (-task.priority, task.id))
        return queued


class ScriptedPipeline:
    """Mock ``DownloadPipeline`` with scripted failures and concurrency probes."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        progress_steps: int = 5,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.delay = delay
        self.progress_steps = progress_steps
        self.gate = gate
        self.calls: Counter[int] = Counter()
        self.started: list[int] = []
        self.active = 0
        self.max_active = 0
        self.started_event = asyncio.Event()
        self.script: dict[int, list[BaseException | PipelineResult]] = {}

    def fail_with(self, track_id: int, *outcomes: BaseException) -> None:
        self.script.setdefault(track_id, []).extend(outcomes)

    async def run(self, task: DownloadTask, *, progress) -> PipelineResult:
        self.calls[task.track_id] += 1
        self.started.append(task.track_id)
        self.started_event.set()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for step in range(self.progress_steps):
                await progress(step, self.progress_steps * 10)
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            outcomes = self.script.get(task.track_id)
            if outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            return make_result(task.track_id)
        finally:
            self.active -= 1


def scheduler_settings(**overrides: object) -> DownloadsSettings:
    defaults: dict[str, object] = {
        "max_parallel": 1,
        "pause_min_s": 0.0,
        "pause_max_s": 0.0,
        "max_attempts": 3,
        "backoff_s": [0.01, 0.01],
    }
    defaults.update(overrides)
    return DownloadsSettings(**defaults)


def build_scheduler(
    repository: FakeDownloadRepository,
    pipeline: ScriptedPipeline,
    **overrides: object,
) -> DownloadScheduler:
    return DownloadScheduler.from_settings(
        repository, pipeline, scheduler_settings(**overrides)
    )


async def run_scheduler(scheduler: DownloadScheduler, timeout: float = 5.0) -> None:
    await scheduler.start()
    try:
        await asyncio.wait_for(scheduler.wait_idle(), timeout=timeout)
    finally:
        await scheduler.stop()


# ---------------------------------------------------------------- repository


async def test_repository_roundtrip_and_unique_track(db: Database) -> None:
    track_ids = [track.id for track in (await TrackRepository(db).list(limit=2))[0]]
    repository = DownloadRepository(db)

    task, created = await repository.add(track_ids[0], priority=3)
    assert created is True
    assert task.status == "queued"
    assert task.priority == 3
    assert task.title  # joined from tracks

    duplicate, created_again = await repository.add(track_ids[0], priority=3)
    assert created_again is False
    assert duplicate.id == task.id

    started = await repository.mark_started(task.id)
    assert started is not None
    assert started.status == "downloading"
    assert started.attempts == 1
    # The claim is atomic: a downloading row cannot be claimed twice.
    assert await repository.mark_started(task.id) is None

    await repository.update_progress(task.id, 500, 1000)
    progress = await repository.get(task.id)
    assert progress is not None
    assert (progress.bytes_done, progress.bytes_total) == (500, 1000)

    target = "Artist/Album (2024)/Disc 1/01 - Track.flac"
    completed = await repository.complete(task.id, target_path=target, bytes_done=1000)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.target_path == target
    assert completed.format == "flac"  # derived from target_path, UI badge
    assert completed.quality is None  # never promised without evidence

    completed_page = await repository.list("completed")
    assert completed_page.total == 1
    assert completed_page.items[0].id == task.id
    assert (await repository.list("queued")).total == 0
    assert (await repository.list()).total == 1

    failed = await repository.fail(task.id, "boom")
    assert failed is not None
    assert (failed.status, failed.error) == ("failed", "boom")

    reset = await repository.reset(task.id, priority=7)
    assert reset is not None
    assert (reset.status, reset.attempts, reset.error, reset.priority) == (
        "queued",
        0,
        None,
        7,
    )

    assert await repository.delete(task.id) is True
    assert await repository.get(task.id) is None


async def test_repository_rejects_unknown_track_and_status(db: Database) -> None:
    repository = DownloadRepository(db)
    with pytest.raises(NotFoundError):
        await repository.add(999_999)
    with pytest.raises(ValueError):
        await repository.list("bogus")


async def test_repository_recover_interrupted(db: Database) -> None:
    track_ids = [track.id for track in (await TrackRepository(db).list(limit=2))[0]]
    repository = DownloadRepository(db)
    low, _ = await repository.add(track_ids[0], priority=0)
    high, _ = await repository.add(track_ids[1], priority=5)
    await repository.mark_started(low.id)

    recovered = await repository.recover_interrupted()
    assert [task.id for task in recovered] == [high.id, low.id]  # priority DESC
    assert all(task.status == "queued" for task in recovered)
    interrupted = await repository.get(low.id)
    assert interrupted is not None
    assert interrupted.status == "queued"


# ----------------------------------------------------------------- scheduler


def test_is_retryable_error_classification() -> None:
    assert is_retryable_error(RetryableDownloadError("busy"))
    assert not is_retryable_error(PermanentDownloadError("forbidden"))
    assert is_retryable_error(TimeoutError())
    assert is_retryable_error(OSError("network down"))
    assert not is_retryable_error(FileNotFoundError("nope"))
    rate_limited = RuntimeError("too many requests")
    rate_limited.status_code = 429
    assert is_retryable_error(rate_limited)
    server_error = RuntimeError("bad gateway")
    server_error.status_code = 502
    assert is_retryable_error(server_error)
    auth_error = RuntimeError("unauthorized")
    auth_error.status_code = 401
    assert not is_retryable_error(auth_error)


async def test_scheduler_priority_order_single_parallel() -> None:
    repository = FakeDownloadRepository()
    low = repository.seed(11, priority=0)
    high = repository.seed(12, priority=5)
    middle = repository.seed(13, priority=1)
    pipeline = ScriptedPipeline(delay=0.005)
    scheduler = build_scheduler(repository, pipeline)

    await run_scheduler(scheduler)

    assert pipeline.started == [12, 13, 11]  # priority DESC, FIFO inside level
    assert pipeline.max_active == 1  # anti-ban: one download at a time
    for task in (low, high, middle):
        stored = repository.tasks[task.id]
        assert stored.status == "completed"
        assert stored.attempts == 1


async def test_scheduler_anti_ban_pause_between_tasks() -> None:
    repository = FakeDownloadRepository()
    repository.seed(1)
    repository.seed(2)
    starts: list[float] = []

    class TimedPipeline(ScriptedPipeline):
        async def run(self, task: DownloadTask, *, progress) -> PipelineResult:
            starts.append(time.monotonic())
            return await super().run(task, progress=progress)

    pipeline = TimedPipeline()
    scheduler = DownloadScheduler(
        repository,
        pipeline,
        pause_min_s=0.05,
        pause_max_s=0.06,
    )
    await run_scheduler(scheduler)

    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.045


async def test_scheduler_retries_retryable_errors_with_backoff() -> None:
    repository = FakeDownloadRepository()
    task = repository.seed(7)
    pipeline = ScriptedPipeline()
    pipeline.fail_with(
        7, RetryableDownloadError("503 busy"), RetryableDownloadError("timeout")
    )
    scheduler = build_scheduler(repository, pipeline, max_attempts=3)

    await run_scheduler(scheduler)

    stored = repository.tasks[task.id]
    assert pipeline.calls[7] == 3
    assert stored.status == "completed"
    assert stored.attempts == 3


async def test_scheduler_does_not_retry_permanent_failures() -> None:
    repository = FakeDownloadRepository()
    task = repository.seed(8)
    pipeline = ScriptedPipeline()
    pipeline.fail_with(8, PermanentDownloadError("404 not found"))
    scheduler = build_scheduler(repository, pipeline, max_attempts=3)

    await run_scheduler(scheduler)

    stored = repository.tasks[task.id]
    assert pipeline.calls[8] == 1
    assert stored.status == "failed"
    assert stored.attempts == 1
    assert "404" in (stored.error or "")


async def test_scheduler_exhausts_attempts_then_fails() -> None:
    repository = FakeDownloadRepository()
    task = repository.seed(9)
    pipeline = ScriptedPipeline()
    pipeline.fail_with(9, *[RetryableDownloadError("boom") for _ in range(5)])
    scheduler = build_scheduler(repository, pipeline, max_attempts=2)

    await run_scheduler(scheduler)

    stored = repository.tasks[task.id]
    assert pipeline.calls[9] == 2
    assert stored.status == "failed"
    assert stored.attempts == 2


async def test_scheduler_4xx_is_not_retried_5xx_is() -> None:
    repository = FakeDownloadRepository()
    task = repository.seed(10)
    pipeline = ScriptedPipeline()
    unavailable = RuntimeError("upstream unavailable")
    unavailable.status_code = 503
    unauthorized = RuntimeError("unauthorized")
    unauthorized.status_code = 401
    pipeline.fail_with(10, unavailable, unauthorized)
    scheduler = build_scheduler(repository, pipeline, max_attempts=3)

    await run_scheduler(scheduler)

    stored = repository.tasks[task.id]
    assert pipeline.calls[10] == 2  # 503 retried, 401 permanent
    assert stored.status == "failed"


async def test_scheduler_recovery_requeues_downloading_tasks() -> None:
    repository = FakeDownloadRepository()
    interrupted = repository.seed(1, status="downloading", attempts=1)
    queued = repository.seed(2, status="queued")
    pipeline = ScriptedPipeline()
    scheduler = build_scheduler(repository, pipeline)

    await run_scheduler(scheduler)

    assert pipeline.calls[1] == 1
    assert pipeline.calls[2] == 1
    assert repository.tasks[interrupted.id].status == "completed"
    assert repository.tasks[interrupted.id].attempts == 2  # interrupted + new one
    assert repository.tasks[queued.id].status == "completed"


async def test_scheduler_shutdown_requeues_running_task() -> None:
    repository = FakeDownloadRepository()
    task = repository.seed(1)
    pipeline = ScriptedPipeline(gate=asyncio.Event())
    scheduler = build_scheduler(repository, pipeline)

    await scheduler.start()
    try:
        await asyncio.wait_for(pipeline.started_event.wait(), timeout=5)
        assert repository.tasks[task.id].status == "downloading"
    finally:
        await scheduler.stop()

    stored = repository.tasks[task.id]
    assert stored.status == "queued"
    assert "interrupted by shutdown" in (stored.error or "")
    assert scheduler.cancel_event.is_set()


async def test_scheduler_skips_task_cancelled_after_enqueue() -> None:
    repository = FakeDownloadRepository()
    task = repository.seed(1)
    stale = task.model_copy(update={"status": "cancelled"})
    repository.tasks[task.id] = stale

    pipeline = ScriptedPipeline()
    scheduler = build_scheduler(repository, pipeline)
    scheduler.enqueue(stale)
    scheduler.enqueue(stale)  # duplicate enqueue is a no-op

    await run_scheduler(scheduler)

    assert pipeline.calls == Counter()  # never claimed
    assert repository.tasks[task.id].status == "cancelled"


# ------------------------------------------------------------------- manager


def build_manager(
    db: Database,
    pipeline: ScriptedPipeline,
    *,
    settings: DownloadsSettings | None = None,
    bus: FakeBus | None = None,
    **kwargs: object,
) -> tuple[DownloadRepository, DownloadScheduler, DownloadManagerService]:
    repository = DownloadRepository(db)
    scheduler = DownloadScheduler.from_settings(
        repository, pipeline, settings or scheduler_settings()
    )
    manager = DownloadManagerService(
        repository=repository,
        scheduler=scheduler,
        bus=bus,
        tracks=TrackRepository(db),
        **kwargs,
    )
    return repository, scheduler, manager


class FakeResolver:
    def __init__(
        self,
        *,
        albums: dict[int, list[int] | None] | None = None,
        playlists: dict[str, list[int] | None] | None = None,
    ) -> None:
        self.albums = albums or {}
        self.playlists = playlists or {}

    async def album_track_ids(self, album_id: int) -> list[int] | None:
        return self.albums.get(album_id)

    async def playlist_track_ids(self, playlist_id: str) -> list[int] | None:
        return self.playlists.get(playlist_id)


async def test_manager_track_end_to_end(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    pipeline = ScriptedPipeline()
    bus = FakeBus()
    verified: list[tuple[int, str]] = []

    async def register(task: DownloadTask, result: PipelineResult) -> int:
        return 41

    async def verify(media_file_id: int, result: PipelineResult) -> None:
        verified.append((media_file_id, result.checksum))

    repository, scheduler, manager = build_manager(
        db,
        pipeline,
        bus=bus,
        register_callback=register,
        verify_callback=verify,
    )
    try:
        task = await manager.start_track(track_id)
        assert task.status == "queued"
        assert bus.count("download.queued") == 1

        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)

        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.target_path == make_result(track_id).path
        assert stored.format == "flac"
        assert verified == [(41, "a" * 64)]
        assert bus.count("download.done") == 1
        assert bus.count("library.updated") == 1
        assert bus.count("download.progress") <= 2  # throttled to <= 2 Hz
    finally:
        await manager.shutdown()


async def test_manager_start_recovers_persisted_queue(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    repository = DownloadRepository(db)
    seeded, created = await repository.add(track_id)
    assert created is True

    pipeline = ScriptedPipeline()
    _repo, scheduler, manager = build_manager(db, pipeline)
    try:
        recovered = await manager.start()
        assert [task.id for task in recovered] == [seeded.id]
        assert await manager.start() == []  # idempotent

        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)
        stored = await repository.get(seeded.id)
        assert stored is not None
        assert stored.status == "completed"
    finally:
        await manager.shutdown()


async def test_manager_normalises_target_path_under_downloads_root(
    db: Database, settings
) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    absolute = settings.paths.downloads / "Artist" / "Album (2024)" / "01 - Track.flac"
    pipeline = ScriptedPipeline()
    pipeline.script[track_id] = [make_result(track_id, path=str(absolute))]
    repository, scheduler, manager = build_manager(
        db, pipeline, downloads_root=settings.paths.downloads
    )
    try:
        task = await manager.start_track(track_id)
        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)
        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.target_path == "Artist/Album (2024)/01 - Track.flac"
        assert stored.format == "flac"
    finally:
        await manager.shutdown()


async def test_manager_duplicate_start_skips_second_queue(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    gate = asyncio.Event()
    pipeline = ScriptedPipeline(gate=gate)
    bus = FakeBus()
    repository, scheduler, manager = build_manager(db, pipeline, bus=bus)
    try:
        first = await manager.start_track(track_id)
        await asyncio.wait_for(pipeline.started_event.wait(), timeout=5)
        second = await manager.start_track(track_id)

        assert second.id == first.id
        assert bus.count("download.queued") == 1
        page = await repository.list()
        assert page.total == 1

        gate.set()
        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)
    finally:
        await manager.shutdown()


async def test_manager_progress_is_throttled(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    pipeline = ScriptedPipeline(progress_steps=200)
    bus = FakeBus()
    _repository, scheduler, manager = build_manager(db, pipeline, bus=bus)
    try:
        await manager.start_track(track_id)
        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)
        assert bus.count("download.progress") <= 2
    finally:
        await manager.shutdown()


async def test_manager_cancel_then_retry(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    gate = asyncio.Event()
    pipeline = ScriptedPipeline(gate=gate)
    repository, scheduler, manager = build_manager(db, pipeline)
    try:
        task = await manager.start_track(track_id)
        await asyncio.wait_for(pipeline.started_event.wait(), timeout=5)

        cancelled = await manager.cancel(task.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status == "cancelled"

        gate.set()
        retried = await manager.retry(task.id)
        assert retried.status == "queued"
        assert retried.attempts == 0
        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)

        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status == "completed"
    finally:
        await manager.shutdown()


async def test_manager_delete_removes_record_and_partial(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    gate = asyncio.Event()
    pipeline = ScriptedPipeline(gate=gate)
    discarded: list[DownloadTask] = []

    async def discard(task: DownloadTask) -> None:
        discarded.append(task)

    repository, _scheduler, manager = build_manager(
        db, pipeline, discard_callback=discard
    )
    try:
        task = await manager.start_track(track_id)
        await asyncio.wait_for(pipeline.started_event.wait(), timeout=5)

        assert await manager.cancel(task.id, remove=True) is None
        assert [item.id for item in discarded] == [task.id]
        assert await repository.get(task.id) is None
        with pytest.raises(NotFoundError):
            await manager.cancel(task.id, remove=True)
    finally:
        await manager.shutdown()


async def test_manager_finished_task_is_not_cancelled_or_retried(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    _repository, scheduler, manager = build_manager(db, ScriptedPipeline())
    try:
        task = await manager.start_track(track_id)
        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)

        with pytest.raises(ConflictError):
            await manager.cancel(task.id)
        with pytest.raises(ConflictError):
            await manager.retry(task.id)
    finally:
        await manager.shutdown()


async def test_manager_post_processing_failure_marks_task_failed(db: Database) -> None:
    track_id = (await TrackRepository(db).list(limit=1))[0][0].id
    bus = FakeBus()

    async def broken_register(task: DownloadTask, result: PipelineResult) -> int:
        raise RuntimeError("media registration failed")

    repository, scheduler, manager = build_manager(
        db, ScriptedPipeline(), bus=bus, register_callback=broken_register
    )
    try:
        task = await manager.start_track(track_id)
        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)

        stored = await repository.get(task.id)
        assert stored is not None
        assert stored.status == "failed"
        assert (stored.error or "").startswith("post-processing failed")
        assert bus.count("download.failed") == 1
        assert bus.count("download.done") == 0
        assert bus.count("library.updated") == 0
    finally:
        await manager.shutdown()


async def test_manager_missing_track_is_404(db: Database) -> None:
    _repository, _scheduler, manager = build_manager(db, ScriptedPipeline())
    try:
        with pytest.raises(NotFoundError):
            await manager.start_track(999_999)
    finally:
        await manager.shutdown()


async def test_manager_album_and_playlist_resolution(db: Database) -> None:
    track_ids = [track.id for track in (await TrackRepository(db).list(limit=2))[0]]
    resolver = FakeResolver(
        albums={17: [track_ids[0], track_ids[1], track_ids[0]]},
        playlists={"1:2": [track_ids[0]]},
    )
    repository, scheduler, manager = build_manager(
        db, ScriptedPipeline(), resolver=resolver
    )
    try:
        album_tasks = await manager.start_album(17)
        assert [task.track_id for task in album_tasks] == track_ids  # deduplicated
        playlist_tasks = await manager.start_playlist("1:2")
        assert [task.track_id for task in playlist_tasks] == [track_ids[0]]

        with pytest.raises(NotFoundError):
            await manager.start_album(404)
        with pytest.raises(NotFoundError):
            await manager.start_playlist("missing:1")

        await asyncio.wait_for(scheduler.wait_idle(), timeout=5)
        assert (await repository.list("completed")).total == 2
    finally:
        await manager.shutdown()


async def test_manager_without_resolver_returns_empty_not_fake(db: Database) -> None:
    repository, _scheduler, manager = build_manager(db, ScriptedPipeline())
    try:
        assert await manager.start_album(17) == []
        assert await manager.start_playlist("1:2") == []
        assert (await repository.list()).total == 0
    finally:
        await manager.shutdown()


# ----------------------------------------------------------------------- API


def make_task(task_id: int = 1, **overrides: object) -> DownloadTask:
    values: dict[str, object] = {
        "id": task_id,
        "track_id": 100 + task_id,
        "title": f"Track {task_id}",
        "status": "queued",
        "priority": 0,
        "attempts": 0,
        "bytes_done": 0,
        "bytes_total": 1000,
        "target_path": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return DownloadTask(**values)


class StubManager:
    """Async stand-in for ``ctx.downloads`` used by route-level tests."""

    def __init__(self) -> None:
        self.tasks: dict[int, DownloadTask] = {1: make_task(1)}
        self.calls: list[tuple] = []

    async def list(self, *, status: str | None = None) -> DownloadTaskPage:
        items = [
            task
            for task in self.tasks.values()
            if status is None or task.status == status
        ]
        return DownloadTaskPage(items=items, total=len(items))

    async def start_track(self, track_id: int, *, priority: int = 0) -> DownloadTask:
        self.calls.append(("track", track_id, priority))
        task = make_task(len(self.tasks) + 1, track_id=track_id)
        self.tasks[task.id] = task
        return task

    async def start_album(self, album_id: int, *, priority: int = 0) -> list[DownloadTask]:
        self.calls.append(("album", album_id, priority))
        return [make_task(2, track_id=album_id)]

    async def start_playlist(
        self, playlist_id: str, *, priority: int = 0
    ) -> list[DownloadTask]:
        self.calls.append(("playlist", playlist_id, priority))
        return [make_task(3, track_id=7)]

    async def cancel(
        self, task_id: int, *, remove: bool = False
    ) -> DownloadTask | None:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("Download task not found", details={"id": task_id})
        self.calls.append(("cancel", task_id, remove))
        if remove:
            self.tasks.pop(task_id, None)
            return None
        if task.status in {"completed", "failed"}:
            raise ConflictError(
                "a finished download cannot be cancelled",
                details={"id": task_id, "status": task.status},
            )
        updated = task.model_copy(update={"status": "cancelled"})
        self.tasks[task_id] = updated
        return updated

    async def retry(self, task_id: int) -> DownloadTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("Download task not found", details={"id": task_id})
        self.calls.append(("retry", task_id))
        if task.status == "completed":
            raise ConflictError(
                "download is already completed",
                details={"id": task_id, "status": task.status},
            )
        updated = task.model_copy(update={"status": "queued", "attempts": 0})
        self.tasks[task_id] = updated
        return updated


def _mount_downloads_router(app) -> None:
    """Mount the P4 router the way main.py will, before the SPA catch-all.

    ``create_app`` cannot know about P4 yet (``main.py`` is shared/frozen in this
    task), and its SPA fallback is registered last. Re-appending the fallback
    keeps ``/api/v1/downloads`` reachable in these route-level tests.
    """

    app.include_router(downloads_api.router, prefix="/api/v1")
    fallback = [
        route
        for route in app.router.routes
        if getattr(route, "path", "") == "/{full_path:path}"
    ]
    for route in fallback:
        app.router.routes.remove(route)
        app.router.routes.append(route)


@contextmanager
def downloads_client(
    tmp_path: Path, *, pin: str | None = None
) -> Iterator[tuple[TestClient, StubManager]]:
    app = create_app(make_settings(tmp_path, seed_catalog=False, pin=pin))
    _mount_downloads_router(app)
    with TestClient(app) as client:
        stub = StubManager()
        app.state.ctx.downloads = stub
        yield client, stub


def test_api_list_downloads(tmp_path: Path) -> None:
    with downloads_client(tmp_path) as (client, stub):
        response = client.get("/api/v1/downloads")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        item = payload["items"][0]
        assert item["title"] == "Track 1"
        assert item["status"] == "queued"
        assert item["format"] is None

        assert client.get(
            "/api/v1/downloads", params={"status": "failed"}
        ).json()["total"] == 0
        assert client.get(
            "/api/v1/downloads", params={"status": "bogus"}
        ).status_code == 422


def test_api_post_downloads_selectors(tmp_path: Path) -> None:
    with downloads_client(tmp_path) as (client, stub):
        track = client.post(
            "/api/v1/downloads", json={"track_id": 501, "priority": 2}
        )
        assert track.status_code == 202
        assert track.json()["tasks"][0]["track_id"] == 501
        assert stub.calls[-1] == ("track", 501, 2)

        album = client.post("/api/v1/downloads", json={"album_id": 17})
        assert album.status_code == 202
        assert stub.calls[-1] == ("album", 17, 0)

        playlist = client.post("/api/v1/downloads", json={"playlist_id": "1:2"})
        assert playlist.status_code == 202
        assert stub.calls[-1] == ("playlist", "1:2", 0)


def test_api_post_downloads_validation(tmp_path: Path) -> None:
    with downloads_client(tmp_path) as (client, _stub):
        empty = client.post("/api/v1/downloads", json={})
        assert empty.status_code == 400
        assert empty.json()["error"]["code"] == "bad_request"

        ambiguous = client.post(
            "/api/v1/downloads", json={"track_id": 1, "album_id": 2}
        )
        assert ambiguous.status_code == 400

        negative = client.post("/api/v1/downloads", json={"track_id": -1})
        assert negative.status_code == 400

        blank_playlist = client.post("/api/v1/downloads", json={"playlist_id": "  "})
        assert blank_playlist.status_code == 400

        no_body = client.post("/api/v1/downloads")
        assert no_body.status_code == 422
        assert no_body.json()["error"]["code"] == "validation_error"


@pytest.mark.skip(reason="PIN feature removed")
def test_api_mutations_require_pin(tmp_path: Path) -> None:
    with downloads_client(tmp_path, pin="1234") as (client, _stub):
        assert client.get("/api/v1/downloads").status_code == 200

        response = client.post("/api/v1/downloads", json={"track_id": 5})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_required"
        assert client.delete("/api/v1/downloads/1").status_code == 401
        assert client.post("/api/v1/downloads/1/cancel").status_code == 401
        assert client.post("/api/v1/downloads/1/retry").status_code == 401

        login = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert login.status_code == 200
        assert client.post(
            "/api/v1/downloads", json={"track_id": 5}
        ).status_code == 202


@pytest.mark.skip(reason="PIN feature removed")
def test_api_mutations_remote_without_pin_are_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "client_is_loopback", lambda request: False)
    with downloads_client(tmp_path) as (client, _stub):
        response = client.post("/api/v1/downloads", json={"track_id": 5})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "pin_required"
        assert client.get("/api/v1/downloads").status_code == 200


def test_api_cancel_retry_delete(tmp_path: Path) -> None:
    with downloads_client(tmp_path) as (client, stub):
        stub.tasks[2] = make_task(2, status="completed")

        cancelled = client.post("/api/v1/downloads/1/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        conflict = client.post("/api/v1/downloads/2/cancel")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "conflict"

        retried = client.post("/api/v1/downloads/1/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "queued"
        assert retried.json()["attempts"] == 0

        assert client.post("/api/v1/downloads/99/cancel").status_code == 404
        assert client.post("/api/v1/downloads/99/retry").status_code == 404
        assert client.delete("/api/v1/downloads/99").status_code == 404

        deleted = client.delete("/api/v1/downloads/1")
        assert deleted.status_code == 204
        assert deleted.text == ""
        assert 1 not in stub.tasks


def test_api_context_fallback_to_download_manager(tmp_path: Path) -> None:
    with downloads_client(tmp_path) as (client, stub):
        context = client.app.state.ctx
        context.downloads = None
        context.download_manager = stub
        assert client.get("/api/v1/downloads").status_code == 200


def test_api_missing_manager_is_503(tmp_path: Path) -> None:
    with downloads_client(tmp_path) as (client, _stub):
        context = client.app.state.ctx
        context.downloads = None
        response = client.get("/api/v1/downloads")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unavailable"


def test_router_is_documented_under_downloads_prefix() -> None:
    paths = {route.path for route in downloads_api.router.routes}
    assert paths == {
        "/downloads",
        "/downloads/{task_id}",
        "/downloads/{task_id}/cancel",
        "/downloads/{task_id}/retry",
    }
