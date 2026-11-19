"""Async download scheduler: one priority queue, anti-ban pauses, bounded retries.

The scheduler is policy-only. It never touches HTTP, files or tags: the actual
download is delegated to a ``DownloadPipeline`` (docs/API.md §9.3). It owns:

* a priority queue (higher ``priority`` first, FIFO inside a priority level);
* at most ``settings.downloads.max_parallel`` concurrent jobs (anti-ban: one);
* a random pause between two consecutive starts (no pause before the first);
* retries with ``settings.downloads.backoff_s`` for retryable failures only
  (429/5xx/network), never for auth/not-found style 4xx errors;
* atomic claim of queued rows, so a task cancelled between enqueue and start is
  skipped instead of being downloaded;
* restart-safe recovery (``downloading`` → ``queued``) and graceful shutdown
  that returns interrupted tasks to the queue instead of failing them.

Resuming a partial file via HTTP Range is the pipeline's job: the scheduler
keeps the ``.part`` file and ``bytes_done`` untouched and simply runs the same
task again.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from itertools import count

from app.config import DownloadsSettings, Settings
from app.core.models import DownloadTask
from app.downloads.base import DownloadPipeline, PipelineResult
from app.downloads.repository import DownloadRepository
from app.logging_setup import get_logger

logger = get_logger("downloads.scheduler")

ProgressHook = Callable[[int, int, int | None], Awaitable[None]]
CompletedHook = Callable[[DownloadTask, PipelineResult], Awaitable[None]]
FailedHook = Callable[[DownloadTask], Awaitable[None]]
RetryHook = Callable[[DownloadTask, float], Awaitable[None]]

# HTTP statuses that are worth retrying: rate limiting, request timeout and
# server-side failures. Everything else in the 4xx family is permanent.
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429})
MAX_ERROR_LENGTH = 500
_URL_PATTERN = re.compile(r"https?://\S+")


class DownloadError(RuntimeError):
    """Base error a pipeline may raise; ``retryable`` drives scheduler policy."""

    retryable = True


class RetryableDownloadError(DownloadError):
    """Transient failure (network, 429, 5xx): retry with backoff."""

    retryable = True


class PermanentDownloadError(DownloadError):
    """Permanent failure (auth, not found, unsupported): never retry."""

    retryable = False


def is_retryable_error(exc: BaseException) -> bool:
    """Classify pipeline failures without importing pipeline internals.

    A pipeline can be explicit (``RetryableDownloadError`` /
    ``PermanentDownloadError``), expose a ``retryable`` flag or carry an HTTP
    ``status_code``/``status`` attribute. Unknown exceptions are retried a
    bounded number of times, because network failures are the common case.
    """

    flag = getattr(exc, "retryable", None)
    if isinstance(flag, bool):
        return flag
    for attribute in ("status_code", "status"):
        status = getattr(exc, attribute, None)
        if isinstance(status, int):
            if status in RETRYABLE_HTTP_STATUS or 500 <= status < 600:
                return True
            if 400 <= status < 500:
                return False
    if isinstance(exc, FileNotFoundError | PermissionError):
        # Local file problems are not fixed by retrying the same request.
        return False
    if isinstance(exc, OSError):
        # socket timeouts, connection resets and similar transient IO errors.
        return True
    name = type(exc).__name__.casefold()
    permanent_markers = ("auth", "forbidden", "notfound", "not_found", "unsupported")
    return not any(marker in name for marker in permanent_markers)


def error_message(exc: BaseException) -> str:
    """Short, redacted failure text safe to persist and to send to clients.

    Signed CDN URLs can appear in HTTP errors; query strings are stripped so a
    download error never leaks a token (AGENTS.md §8).
    """

    message = str(exc).strip() or type(exc).__name__
    message = _URL_PATTERN.sub("[url]", message)
    return message[:MAX_ERROR_LENGTH]


class DownloadScheduler:
    """Priority queue + workers driving ``DownloadPipeline.run``."""

    def __init__(
        self,
        repository: DownloadRepository,
        pipeline: DownloadPipeline,
        *,
        max_parallel: int = 1,
        pause_min_s: float = 2.0,
        pause_max_s: float = 5.0,
        max_attempts: int = 3,
        backoff_s: Sequence[float] = (5.0, 30.0, 120.0),
        on_progress: ProgressHook | None = None,
        on_completed: CompletedHook | None = None,
        on_failed: FailedHook | None = None,
        on_retry: RetryHook | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._repository = repository
        self._pipeline = pipeline
        self._max_parallel = max(1, max_parallel)
        self._pause_min_s = max(0.0, pause_min_s)
        self._pause_max_s = max(0.0, pause_max_s)
        self._max_attempts = max(1, max_attempts)
        self._backoff_s = [max(0.0, delay) for delay in backoff_s]
        self._on_progress = on_progress
        self._on_completed = on_completed
        self._on_failed = on_failed
        self._on_retry = on_retry
        self._rng = rng or random.Random()

        self._queue: asyncio.PriorityQueue[tuple[int, int, int]] = asyncio.PriorityQueue()
        self._sequence = count()
        self._pending: set[int] = set()
        self._jobs: dict[int, asyncio.Task[None]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._gate = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_start_at = 0.0
        self._started = False
        self._closed = False
        # Set by ``stop()``; lets workers drop a pending anti-ban sleep instead
        # of waiting out the configured pause during shutdown.
        self.cancel_event = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def from_settings(
        cls,
        repository: DownloadRepository,
        pipeline: DownloadPipeline,
        settings: Settings | DownloadsSettings,
        **hooks: object,
    ) -> DownloadScheduler:
        """Build from ``settings.downloads`` (the documented config source)."""

        config = getattr(settings, "downloads", settings)
        return cls(
            repository,
            pipeline,
            max_parallel=config.max_parallel,
            pause_min_s=config.pause_min_s,
            pause_max_s=config.pause_max_s,
            max_attempts=config.max_attempts,
            backoff_s=config.backoff_s,
            **hooks,
        )

    def bind(
        self,
        *,
        on_progress: ProgressHook | None = None,
        on_completed: CompletedHook | None = None,
        on_failed: FailedHook | None = None,
        on_retry: RetryHook | None = None,
    ) -> None:
        """Attach manager hooks after construction (breaks wiring cycles)."""

        if on_progress is not None:
            self._on_progress = on_progress
        if on_completed is not None:
            self._on_completed = on_completed
        if on_failed is not None:
            self._on_failed = on_failed
        if on_retry is not None:
            self._on_retry = on_retry

    async def start(self) -> list[DownloadTask]:
        """Spawn workers and re-queue tasks interrupted by a previous process."""

        if self._closed:
            raise RuntimeError("download scheduler is already stopped")
        async with self._start_lock:
            if self._started:
                return []
            self._started = True
            recovered = await self._repository.recover_interrupted()
            for task in recovered:
                self.enqueue(task)
            self._workers = [
                asyncio.create_task(self._worker(index), name=f"download-worker-{index}")
                for index in range(self._max_parallel)
            ]
            logger.info(
                "download scheduler started: workers=%s recovered=%s",
                self._max_parallel,
                len(recovered),
            )
            return recovered

    async def stop(self) -> None:
        """Graceful shutdown: running tasks go back to ``queued``, then stop."""

        if self._closed and not self._started:
            return
        self._closed = True
        self.cancel_event.set()
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            # ``_execute`` turns cancellation into ``queued``; wait for the DB
            # write before tearing the workers down.
            await asyncio.gather(*jobs, return_exceptions=True)
        workers = list(self._workers)
        self._workers.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._drain_queue()
        self._started = False
        logger.info("download scheduler stopped")

    # ---------------------------------------------------------------- queueing

    def enqueue(self, task: DownloadTask) -> None:
        """Add a task to the in-memory priority queue (idempotent per task id)."""

        if self._closed or task.id in self._pending or task.id in self._jobs:
            return
        self._pending.add(task.id)
        self._queue.put_nowait((-task.priority, next(self._sequence), task.id))

    async def wait_idle(self) -> None:
        """Wait until the queue has no unfinished item (tests/administrative)."""

        await self._queue.join()

    async def cancel(self, task_id: int) -> bool:
        """Cancel the running job of ``task_id``; the caller persists status.

        Cancellation is cooperative only through ``asyncio.Task.cancel``: the
        pipeline sees ``CancelledError`` at its next await point. Returns
        whether a job was actually running.
        """

        job = self._jobs.get(task_id)
        if job is None:
            return False
        job.cancel()
        try:
            await job
        except asyncio.CancelledError:  # pragma: no cover - job swallows it
            pass
        return True

    @property
    def active_count(self) -> int:
        return len(self._jobs)

    # ---------------------------------------------------------------- workers

    async def _worker(self, index: int) -> None:
        while not self._closed:
            try:
                _priority, _sequence, task_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                self._pending.discard(task_id)
                if self._closed:
                    continue
                await self._wait_pause()
                if self._closed:
                    continue
                try:
                    await self._run_job(task_id)
                except Exception:  # pragma: no cover - defensive: keep workers alive
                    logger.exception("download job %s crashed", task_id)
            finally:
                self._queue.task_done()
        logger.debug("download worker %s finished", index)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
        self._pending.clear()

    async def _wait_pause(self) -> None:
        """Globally serialised anti-ban pause between two consecutive starts.

        The lock also throttles ``max_parallel=2``: starts are spaced out even
        though two downloads may overlap. The first task starts immediately.
        """

        loop = asyncio.get_running_loop()
        async with self._gate:
            now = loop.time()
            delay = self._next_start_at - now
            if delay > 0:
                try:
                    await asyncio.wait_for(self.cancel_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                else:  # shutting down: the caller checks ``_closed`` and skips
                    return
            self._next_start_at = loop.time() + self._sample_pause()

    def _sample_pause(self) -> float:
        low, high = sorted((self._pause_min_s, self._pause_max_s))
        if high <= 0:
            return 0.0
        return self._rng.uniform(low, high)

    async def _run_job(self, task_id: int) -> None:
        job = asyncio.create_task(self._execute(task_id), name=f"download-task-{task_id}")
        self._jobs[task_id] = job
        try:
            await job
        finally:
            self._jobs.pop(task_id, None)

    # --------------------------------------------------------------- attempts

    async def _execute(self, task_id: int) -> None:
        try:
            await self._attempt_loop(task_id)
        except asyncio.CancelledError:
            # Shutdown: keep the partial file and put the task back in queue.
            # Per-task cancel: the manager already persisted the new status.
            if self._closed:
                await self._safe_requeue(task_id)
            return

    async def _attempt_loop(self, task_id: int) -> None:
        while True:
            if self._closed:
                await self._safe_requeue(task_id)
                return
            task = await self._repository.mark_started(task_id)
            if task is None:
                # Cancelled, deleted or already finished between pop and claim.
                return
            try:
                result = await self._pipeline.run(
                    task, progress=self._progress_callback(task_id)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if await self._handle_failure(task, exc):
                    continue
                return
            await self._handle_success(task, result)
            return

    def _progress_callback(self, task_id: int) -> ProgressHook:
        async def report(bytes_done: int, bytes_total: int | None) -> None:
            hook = self._on_progress
            if hook is not None:
                await hook(task_id, max(bytes_done, 0), bytes_total)

        return report

    async def _handle_success(self, task: DownloadTask, result: PipelineResult) -> None:
        if self._on_completed is not None:
            await self._on_completed(task, result)
            return
        # Standalone scheduler (no manager): still leave a consistent row.
        await self._repository.complete(
            task.id, target_path=result.path, bytes_done=result.bytes_written
        )

    async def _handle_failure(self, task: DownloadTask, exc: BaseException) -> bool:
        """Persist the failure and either schedule a retry or give up.

        Returns ``True`` when the attempt loop should run the task again.
        """

        current = await self._repository.get(task.id) or task
        if current.status in {"cancelled", "completed", "paused"}:
            return False
        error = error_message(exc)
        attempts = current.attempts
        if not is_retryable_error(exc) or attempts >= self._max_attempts:
            failed = await self._repository.fail(task.id, error)
            if failed is not None and self._on_failed is not None:
                await self._on_failed(failed)
            return False

        queued = await self._repository.requeue(task.id, error=error)
        delay = self._retry_delay(attempts)
        logger.info(
            "download %s attempt %s failed (%s); retrying in %.1fs",
            task.id,
            attempts,
            error,
            delay,
        )
        if queued is not None and self._on_retry is not None:
            await self._on_retry(queued, delay)
        if delay > 0:
            await asyncio.sleep(delay)
        return True

    def _retry_delay(self, attempts: int) -> float:
        if not self._backoff_s:
            return 0.0
        return self._backoff_s[min(max(attempts - 1, 0), len(self._backoff_s) - 1)]

    async def _safe_requeue(self, task_id: int) -> None:
        try:
            await self._repository.requeue(task_id, error="interrupted by shutdown")
        except Exception:  # pragma: no cover - shutdown must never raise
            logger.exception("cannot requeue interrupted download %s", task_id)
