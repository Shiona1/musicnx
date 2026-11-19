"""P2 import/scanner orchestration: probe -> dedupe -> verify -> cover cache.

One job at a time. All blocking IO (walk, mutagen, hashing, Pillow) runs in
worker threads; the event loop only awaits. Nothing here talks to the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.config import Settings, resolved_import_roots
from app.core.event_bus import EventBus
from app.core.models import ImportOutcome
from app.core.timeutils import utc_now_iso
from app.database.repositories import LibraryRepository
from app.errors import AppError, ConflictError, NotFoundError
from app.library.covers import CoverCache
from app.library.probe import (
    MediaProbeError,
    discover_media_files,
    probe_media,
)
from app.logging_setup import get_logger

logger = get_logger("library.importer")

ImportStatus = Literal[
    "queued", "running", "verifying", "cancelling", "completed", "failed", "cancelled"
]


@dataclass
class ImportJob:
    id: str
    status: ImportStatus = "queued"
    phase: str = "queued"
    total: int = 0
    processed: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    missing: int = 0
    verify_total: int = 0
    verify_processed: int = 0
    errors: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    target_paths: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False

    def as_dict(self) -> dict[str, Any]:
        percent = 0
        if self.status in {"verifying"} and self.verify_total:
            percent = int(self.verify_processed * 100 / self.verify_total)
        elif self.total:
            percent = int(self.processed * 100 / self.total)
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "total": self.total,
            "processed": self.processed,
            "imported": self.imported,
            "updated": self.updated,
            "skipped": self.skipped,
            "missing": self.missing,
            "verify_total": self.verify_total,
            "verify_processed": self.verify_processed,
            "percent": min(percent, 100),
            "errors": list(self.errors),
            "roots": list(self.roots),
            "target_paths": list(self.target_paths),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
        }


class ImportManager:
    def __init__(
        self,
        *,
        settings: Settings,
        bus: EventBus,
        library: LibraryRepository,
        covers: CoverCache,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._library = library
        self._covers = covers
        self._roots = resolved_import_roots(settings)
        self._task: asyncio.Task[None] | None = None
        self._job: ImportJob | None = None
        self._lock = asyncio.Lock()

    @property
    def roots(self) -> list[Path]:
        return self._roots

    def status(self) -> dict[str, Any] | None:
        return self._job.as_dict() if self._job is not None else None

    async def start(self, raw_paths: list[str] | None = None) -> ImportJob:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise ConflictError("an import is already running", code="import_running")
            self._ensure_roots_exist()
            targets = self._validate_targets(raw_paths or [])
            job = ImportJob(
                id=uuid.uuid4().hex,
                roots=[str(root) for root in self._roots],
                target_paths=[str(path) for path in targets],
            )
            self._job = job
            self._task = asyncio.create_task(self._run(job, targets), name="library-import")
            return job

    async def cancel(self) -> ImportJob:
        async with self._lock:
            if self._task is None or self._task.done() or self._job is None:
                raise ConflictError("no import is running", code="import_not_running")
            self._job.cancel_requested = True
            self._job.status = "cancelling"
            return self._job

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            if self._job is not None and self._job.status not in {
                "completed",
                "failed",
                "cancelled",
            }:
                self._job.status = "cancelled"
                self._job.finished_at = utc_now_iso()

    # ------------------------------------------------------------------ internals

    def _ensure_roots_exist(self) -> None:
        missing = [str(root) for root in self._roots if not root.is_dir()]
        if missing:
            raise ConflictError(
                "one or more import roots do not exist",
                code="import_root_missing",
                details={"roots": missing},
            )

    def _validate_targets(self, raw_paths: list[str]) -> list[Path]:
        targets: list[Path] = []
        for raw in raw_paths:
            candidate = Path(raw).expanduser().resolve()
            if not any(candidate.is_relative_to(root) for root in self._roots):
                raise AppError(
                    "import path is outside the configured library roots",
                    code="path_not_allowed",
                    status_code=403,
                    details={"path": str(candidate), "roots": [str(r) for r in self._roots]},
                )
            if not candidate.exists():
                raise NotFoundError(
                    "import path does not exist", details={"path": str(candidate)}
                )
            targets.append(candidate)
        return targets

    async def _run(self, job: ImportJob, targets: list[Path]) -> None:
        try:
            job.status = "running"
            job.phase = "discovering"
            job.started_at = utc_now_iso()
            await self._publish("library.import.started", job=job)

            files = await self._discover(job, targets)
            job.phase = "importing"
            job.total = len(files)
            source_id = await self._library.get_or_create_source("local")
            media_to_verify: list[int] = []

            for index, path in enumerate(files, start=1):
                if job.cancel_requested:
                    break
                try:
                    probe = await asyncio.to_thread(probe_media, path, self._root_for(path))
                    outcome = await self._library.import_local(probe.metadata, source_id)
                    self._count_outcome(job, outcome)
                    if probe.cover:
                        await self._store_cover(outcome, probe.cover)
                    media_to_verify.append(outcome.media_file_id)
                except MediaProbeError as exc:
                    self._record_error(job, f"{path.name}: {exc}")
                except Exception as exc:  # one bad file must not abort the scan
                    logger.exception("import failed for %s", path)
                    self._record_error(job, f"{path.name}: {exc}")
                job.processed = index
                await self._publish("library.import.progress", job=job)

            if not job.cancel_requested and media_to_verify:
                await self._verify_media(job, media_to_verify)

            if job.cancel_requested:
                job.status = "cancelled"
                job.phase = "cancelled"
                await self._publish("library.import.cancelled", job=job)
            else:
                job.status = "completed"
                job.phase = "done"
                await self._publish("library.import.completed", job=job)
                await self._publish(
                    "library.updated",
                    tracks=job.imported + job.updated,
                    missing=job.missing,
                )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.phase = "cancelled"
            job.finished_at = utc_now_iso()
            await self._publish("library.import.cancelled", job=job)
            raise
        except Exception as exc:
            logger.exception("import job failed")
            job.status = "failed"
            job.phase = "failed"
            job.errors.append(str(exc))
            await self._publish("library.import.failed", job=job, error=str(exc))
        finally:
            job.finished_at = utc_now_iso()
            if job.status in {"completed", "failed", "cancelled"}:
                await self._publish("library.import.progress", job=job)

    async def _discover(self, job: ImportJob, targets: list[Path]) -> list[Path]:
        if targets:
            files: list[Path] = []
            for target in targets:
                if target.is_file():
                    files.append(target)
                else:
                    files.extend(
                        await asyncio.to_thread(
                            discover_media_files, [target], self._settings.library.extensions
                        )
                    )
            unique = sorted(set(files))
        else:
            unique = await asyncio.to_thread(
                discover_media_files, self._roots, self._settings.library.extensions
            )

        if not targets:
            discovered = set(unique)
            known = await self._library.known_media_files(self._roots)
            for row in known:
                path = Path(row["path"]).resolve()
                if path not in discovered and not path.exists():
                    await self._library.set_missing(int(row["id"]), utc_now_iso())
                    job.missing += 1
        return unique

    def _root_for(self, path: Path) -> Path:
        for root in self._roots:
            if path.is_relative_to(root):
                return root
        return self._roots[0]

    @staticmethod
    def _count_outcome(job: ImportJob, outcome: ImportOutcome) -> None:
        if outcome.status == "created":
            job.imported += 1
        elif outcome.status == "updated":
            job.updated += 1
        else:
            job.skipped += 1

    async def _store_cover(self, outcome: ImportOutcome, data: bytes) -> None:
        album_id = outcome.album_id or await self._library.album_for_track(outcome.track_id)
        if album_id is None:
            return
        current_key = await self._library.album_cover(album_id)
        # A non-empty cover_path is only valid while the 300px cache file exists.
        if current_key and self._covers.path_for(current_key, 300).is_file():
            return
        try:
            key, evicted = await self._covers.store(data)
        except Exception as exc:
            logger.warning("cannot store cover for album %s: %s", album_id, exc)
            return
        for evicted_key in evicted:
            await self._library.clear_cover_key(evicted_key)
        if key in evicted:
            # The cache is smaller than this one cover set; do not leave a
            # dangling albums.cover_path pointing at a deleted file.
            logger.warning("cover cache too small for album %s; artwork skipped", album_id)
            return
        await self._library.update_album_cover(album_id, key)

    async def _verify_media(self, job: ImportJob, media_ids: list[int]) -> None:
        job.phase = "verifying"
        job.status = "verifying"
        job.verify_total = len(media_ids)
        job.verify_processed = 0
        await self._publish("library.import.progress", job=job)
        for index, media_id in enumerate(media_ids, start=1):
            if job.cancel_requested:
                return
            row = await self._library.get_media_file(media_id)
            if row is not None and row["verified_at"] is None:
                path = Path(row["path"])
                if not path.is_file():
                    await self._library.set_missing(media_id, utc_now_iso())
                    job.missing += 1
                else:
                    try:
                        checksum = await asyncio.to_thread(
                            self._hash_file, path, self._settings.library.checksum_chunk_bytes
                        )
                    except OSError as exc:
                        self._record_error(job, f"{path.name}: checksum failed: {exc}")
                    else:
                        await self._library.set_verified(
                            media_id, checksum, utc_now_iso()
                        )
            job.verify_processed = index
            await self._publish("library.import.progress", job=job)

    @staticmethod
    def _hash_file(path: Path, chunk_size: int) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _record_error(job: ImportJob, message: str) -> None:
        job.errors.append(message)
        if len(job.errors) > 20:
            del job.errors[: len(job.errors) - 20]

    async def _publish(self, event_type: str, **payload: Any) -> None:
        if "job" in payload:
            payload["job"] = payload["job"].as_dict()
        await self._bus.publish(event_type, payload)
