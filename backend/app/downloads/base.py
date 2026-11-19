"""Frozen P4 download manager contract (docs/API.md §8)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.core.models import DownloadTask, DownloadTaskPage

ProgressCallback = Callable[[int, int | None], Awaitable[None]]


@dataclass(frozen=True)
class SourceFile:
    """Resolved Yandex file info, without persisting the signed URL."""

    url: str
    codec: str
    quality: str
    transport: str  # raw | encraw
    key: str | None = None
    preview: bool = False
    cover_url: str | None = None


@dataclass(frozen=True)
class PipelineResult:
    """What the pipeline wrote; the manager persists these facts."""

    path: str
    format: str
    quality: str
    checksum: str
    bytes_written: int
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    albumartist: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    label: str | None = None


class DownloadSource(Protocol):
    async def resolve(self, track_id: int) -> SourceFile: ...


class DownloadPipeline(Protocol):
    async def run(
        self, task: DownloadTask, *, progress: ProgressCallback
    ) -> PipelineResult: ...



class DownloadManager(Protocol):
    async def start_track(
        self, track_id: int, *, priority: int = 0
    ) -> DownloadTask: ...

    async def start_album(
        self, album_id: int, *, priority: int = 0
    ) -> list[DownloadTask]: ...

    async def start_playlist(
        self, playlist_id: str, *, priority: int = 0
    ) -> list[DownloadTask]: ...

    async def list(self, *, status: str | None = None) -> DownloadTaskPage: ...

    async def cancel(
        self, task_id: int, *, remove: bool = False
    ) -> DownloadTask | None: ...

    async def retry(self, task_id: int) -> DownloadTask: ...

    async def shutdown(self) -> None: ...
