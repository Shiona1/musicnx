"""Deterministic mock download pipeline (P4 mock-first, no network)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from app.config import Settings
from app.core.models import DownloadTask
from app.database.repositories import TrackRepository
from app.downloads.base import PipelineResult, ProgressCallback, SourceFile
from app.downloads.integrity import sha256_file
from app.downloads.paths import PathContext, ensure_within_root, render_relative_path
from app.downloads.quality import classify_codec
from app.downloads.tags import TrackTags, write_tags

ASSET = Path(__file__).resolve().parent.parent / "assets" / "mock_download.mp3"


class _MockSource:
    async def resolve(self, track_id: int) -> SourceFile:
        return SourceFile(
            url="mock://asset",
            codec="mp3",
            quality="mp3",
            transport="raw",
        )


class MockDownloadPipeline:
    """Copy a bundled tagged MP3 into the downloads layout.

    Mock is explicitly virtual for network/quality, but the resulting file is a
    real playable MP3 with checksum and offline registration.
    """

    def __init__(self, settings: Settings, tracks: TrackRepository) -> None:
        self._settings = settings
        self._tracks = tracks
        self._source = _MockSource()

    def part_path(self, task_id: int) -> Path:
        return self._settings.paths.downloads / f".{task_id}.part"

    async def discard_partial(self, task_id: int) -> bool:
        path = self.part_path(task_id)
        existed = await asyncio.to_thread(path.is_file)
        if existed:
            await asyncio.to_thread(path.unlink)
        return existed

    async def close(self) -> None:
        return None

    async def run(
        self, task: DownloadTask, *, progress: ProgressCallback
    ) -> PipelineResult:
        track = await self._tracks.get(task.track_id)
        title = task.title or (track.title if track else f"Track {task.track_id}")
        artists = (
            tuple(a.name for a in track.artists)
            if track and track.artists
            else ("Unknown Artist",)
        )
        album = track.album.title if track and track.album else None
        year = track.album.year if track and track.album else None
        source = await self._source.resolve(task.track_id)
        codec = classify_codec(source.codec)
        ext = codec.extension
        relative = render_relative_path(
            self._settings.downloads.path_template,
            PathContext(
                artists=artists[0],
                album=album,
                year=year,
                disc=1,
                track=task.id,
                title=title,
                ext=ext,
            ),
        )
        root = self._settings.paths.downloads.resolve()
        final = ensure_within_root(root, root / relative)
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
        part = self.part_path(task.id)
        data = await asyncio.to_thread(ASSET.read_bytes)
        await asyncio.to_thread(part.write_bytes, data)
        await progress(0, len(data))
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(part.replace, final)
        metadata = TrackTags(
            title=track.title if track else title,
            artists=artists,
            album=album,
            albumartist=artists[0],
            year=year,
            genre=track.genre if track else None,
            label=track.label if track else None,
        )
        await asyncio.to_thread(write_tags, final, metadata)
        digest = await asyncio.to_thread(sha256_file, final)
        size = await asyncio.to_thread(lambda: final.stat().st_size)
        await progress(size, size)
        return PipelineResult(
            path=str(final),
            format=codec.format,
            quality=codec.family or "mp3",
            checksum=digest,
            bytes_written=size,
            title=metadata.title,
            artists=metadata.artists,
            album=metadata.album,
            albumartist=metadata.albumartist,
            year=metadata.year,
            genre=metadata.genre,
            label=metadata.label,
        )


def checksum(path: Path) -> str:
    """Small helper kept for tests that need the same digest without I/O caching."""

    return hashlib.sha256(path.read_bytes()).hexdigest()
