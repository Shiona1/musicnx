"""P4 streaming download pipeline (docs/API.md §8.3, docs/DOWNLOADS.md §3).

The pipeline never creates a Yandex client: an injected ``DownloadSource``
resolves a track to one signed ``SourceFile``. Metadata for tags/paths comes
from an optional provider (P3 repository adapter); without one the pipeline
still downloads using a conservative fallback.

Guarantees:

* HTTP chunks are streamed by aiohttp and written without buffering the track;
* ``.part`` in the downloads root is fsynced and atomically renamed;
* ``encraw`` is decrypted chunk-by-chunk with AES-128-CTR;
* mutagen, Pillow and SHA-256 run in worker threads, never on the event loop;
* signed URLs and keys are never logged.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

from app.config import Settings
from app.core.models import DownloadTask
from app.downloads.base import (
    DownloadSource,
    PipelineResult,
    ProgressCallback,
    SourceFile,
)
from app.downloads.decrypt import AesCtrDecryptor
from app.downloads.integrity import verify_file
from app.downloads.paths import (
    PathContext,
    ensure_within_root,
    render_relative_path,
)
from app.downloads.quality import (
    SourceSelection,
    describe_source,
)
from app.downloads.tags import TagWriteReport, TrackTags, write_tags
from app.logging_setup import get_logger

logger = get_logger("downloads.pipeline")

DEFAULT_CHUNK_BYTES = 256 * 1024
COVER_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_COVER_HOSTS = frozenset({"avatars.yandex.net"})

MetadataProvider = Callable[[int], Awaitable[TrackTags | None]]


class DownloadError(RuntimeError):
    """Pipeline failure; policy flags drive the scheduler's retry decision."""

    keep_partial = False
    retryable = False


class DownloadHttpError(DownloadError):
    """Upstream answered with an unacceptable HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.retryable = status in {408, 425, 429} or status >= 500
        self.keep_partial = self.retryable
        super().__init__(f"upstream returned HTTP {status}")


class DownloadNetworkError(DownloadError):
    """Transport failure; the partial file stays for a Range retry."""

    keep_partial = True
    retryable = True

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"network error: {reason}")


class DownloadIncompleteError(DownloadError):
    """The body ended before the advertised total; resume from ``.part``."""

    keep_partial = True
    retryable = True

    def __init__(self, done: int, total: int) -> None:
        self.done = done
        self.total = total
        super().__init__(f"incomplete download: {done} of {total} bytes")


class _RestartWithoutRange(Exception):
    """Upstream rejected our Range (416); retry once from byte zero."""


class _Progress:
    """Throttled progress emitter; a broken callback cannot fail a download."""

    def __init__(self, callback: ProgressCallback, interval_s: float) -> None:
        self._callback = callback
        self._interval_s = max(0.0, interval_s)
        self._last_emit = 0.0
        self._last_state: tuple[int, int | None] | None = None

    async def emit(self, done: int, total: int | None, *, force: bool = False) -> None:
        if self._last_state == (done, total):
            return
        now = time.monotonic()
        if not force and done != total and (now - self._last_emit) < self._interval_s:
            return
        self._last_emit = now
        self._last_state = (done, total)
        try:
            await self._callback(done, total)
        except Exception as exc:  # progress is advisory, never fatal
            logger.warning("download progress callback failed: %s", type(exc).__name__)


class DownloadPipeline:
    """Streaming, mock-friendly implementation of the frozen pipeline contract."""

    def __init__(
        self,
        settings: Settings,
        source: DownloadSource,
        *,
        metadata_provider: MetadataProvider | None = None,
        session: Any | None = None,
        cover_hosts: Collection[str] | None = None,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        progress_interval_s: float = 0.5,
        timeout_s: float = 60.0,
    ) -> None:
        self._settings = settings
        self._source = source
        self._metadata_provider = metadata_provider
        self._root = settings.paths.downloads
        self._session = session
        self._owns_session = session is None
        self._cover_hosts = frozenset(
            host.strip().lower()
            for host in (cover_hosts if cover_hosts is not None else DEFAULT_COVER_HOSTS)
            if host and host.strip()
        )
        self._chunk_bytes = max(int(chunk_bytes), 1024)
        self._progress_interval_s = progress_interval_s
        self._timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=timeout_s, sock_read=timeout_s
        )

    @property
    def downloads_root(self) -> Path:
        return self._root

    def part_path(self, task_id: int) -> Path:
        """Resumable partial file for a task (manager discard callbacks use it)."""

        return self._root / f".{task_id}.part"

    async def discard_partial(self, task_id: int) -> bool:
        """Remove a task's ``.part`` file; returns whether it existed."""

        path = self.part_path(task_id)
        existed = await asyncio.to_thread(path.is_file)
        if existed:
            await asyncio.to_thread(_unlink, path)
        return existed

    async def close(self) -> None:
        """Close the internally created aiohttp session (injected ones are kept)."""

        if self._owns_session and self._session is not None:
            if not getattr(self._session, "closed", False):
                await self._session.close()
            self._session = None

    async def run(
        self, task: DownloadTask, *, progress: ProgressCallback
    ) -> PipelineResult:
        """Resolve, stream, decrypt, tag and verify one track."""

        throttle = _Progress(progress, self._progress_interval_s)
        part_path = self._part_path(task)
        final_path: Path | None = None
        renamed = False
        try:
            source = await self._source.resolve(task.track_id)
            selection = describe_source(
                source, self._settings.downloads.quality_order
            )
            if selection.preview:
                raise DownloadError("only a 30-second preview is available")
            self._log_selection(task, selection)
            metadata = await self._load_metadata(task)
            final_path = await asyncio.to_thread(
                self._target_path, metadata, selection
            )

            await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
            await self._download(source, selection, part_path, throttle)

            await asyncio.to_thread(_replace_atomic, part_path, final_path)
            renamed = True

            cover = await self._prepare_cover(source)
            tag_report = await asyncio.to_thread(
                write_tags, final_path, metadata, cover=cover
            )
            self._log_tag_report(task, tag_report)

            if cover is not None:
                try:
                    await asyncio.to_thread(
                        _write_cover_file, final_path.parent / "cover.jpg", cover
                    )
                except OSError as exc:
                    logger.warning(
                        "cannot write cover.jpg for track %s: %s",
                        task.id,
                        type(exc).__name__,
                    )

            report = await asyncio.to_thread(
                verify_file,
                final_path,
                expected_container=selection.codec.container,
            )
            # Tags and cover can change the file length; the last progress event
            # reports the size of the file that is actually on disk.
            await throttle.emit(report.size, report.size, force=True)
            return PipelineResult(
                path=str(report.path),
                format=selection.codec.format,
                quality=selection.actual_quality,
                checksum=report.sha256,
                bytes_written=report.size,
                title=metadata.title,
                artists=metadata.artists,
                album=metadata.album,
                albumartist=metadata.albumartist,
                year=metadata.year,
                genre=metadata.genre,
                track_number=metadata.track_number,
                disc_number=metadata.disc_number,
                label=metadata.label,
            )
        except asyncio.CancelledError:
            # Soft cancel: keep ``.part`` so the manager can resume the task.
            raise
        except Exception as exc:
            if renamed and final_path is not None:
                await asyncio.to_thread(_unlink, final_path)
            elif not _keep_partial(exc):
                await asyncio.to_thread(_unlink, part_path)
            raise

    # ------------------------------------------------------------------ source

    def _log_selection(self, task: DownloadTask, selection: SourceSelection) -> None:
        if selection.preview:
            logger.warning(
                "track %s resolved to a preview stream; saving the factual preview",
                task.id,
            )
        if selection.fell_back:
            logger.info(
                "track %s fell back to %s (requested %s, codec %s)",
                task.id,
                selection.actual_quality,
                selection.requested_quality,
                selection.codec.raw,
            )

    async def _load_metadata(self, task: DownloadTask) -> TrackTags:
        fallback = TrackTags(
            title=(task.title or "").strip() or f"Track {task.track_id}",
            artists=("Unknown Artist",),
        )
        provider = self._metadata_provider
        if provider is None:
            return fallback
        try:
            metadata = await provider(task.track_id)
        except Exception as exc:
            logger.warning(
                "metadata provider failed for track %s: %s",
                task.track_id,
                type(exc).__name__,
            )
            return fallback
        if metadata is None:
            return fallback
        artists = tuple(
            name.strip() for name in metadata.artists if name and name.strip()
        ) or fallback.artists
        return TrackTags(
            title=(metadata.title or "").strip() or fallback.title,
            artists=artists,
            album=metadata.album,
            albumartist=metadata.albumartist,
            year=metadata.year,
            genre=metadata.genre,
            track_number=metadata.track_number,
            disc_number=metadata.disc_number,
            label=metadata.label,
        )

    def _target_path(self, metadata: TrackTags, selection: SourceSelection) -> Path:
        context = PathContext(
            artists=metadata.artists[0] if metadata.artists else "Unknown Artist",
            album=metadata.album,
            year=metadata.year,
            disc=metadata.disc_number,
            track=metadata.track_number,
            title=metadata.title,
            ext=selection.codec.extension,
        )
        relative = render_relative_path(
            self._settings.downloads.path_template, context
        )
        final_path = ensure_within_root(self._root, relative)
        return _unique_path(final_path)

    def _part_path(self, task: DownloadTask) -> Path:
        return self.part_path(task.id)

    # ---------------------------------------------------------------- download

    async def _download(
        self,
        source: SourceFile,
        selection: SourceSelection,
        part_path: Path,
        throttle: _Progress,
    ) -> tuple[int, int | None]:
        session = await self._ensure_session()
        resume_from = await asyncio.to_thread(_file_size, part_path)
        restarted = False
        while True:
            headers = {"Accept-Encoding": "identity"}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
            try:
                written, total = await self._stream(
                    session,
                    source,
                    selection,
                    part_path,
                    headers,
                    resume_from,
                    throttle,
                )
                return written, total
            except _RestartWithoutRange:
                if restarted:
                    raise DownloadHttpError(416) from None
                restarted = True
                await asyncio.to_thread(_unlink, part_path)
                resume_from = 0

    async def _stream(
        self,
        session: Any,
        source: SourceFile,
        selection: SourceSelection,
        part_path: Path,
        headers: dict[str, str],
        resume_from: int,
        throttle: _Progress,
    ) -> tuple[int, int | None]:
        try:
            async with session.get(
                source.url, headers=headers, allow_redirects=True
            ) as response:
                status = int(getattr(response, "status", 0))
                if status == 416 and resume_from > 0:
                    raise _RestartWithoutRange from None
                if status not in {200, 206}:
                    raise DownloadHttpError(status)
                if status == 206 and resume_from > 0:
                    start = _content_range_start(response.headers)
                    if start is not None and start != resume_from:
                        # A 206 for a different range cannot be appended safely.
                        raise _RestartWithoutRange from None

                if resume_from > 0 and status == 200:
                    # The CDN ignored Range: start over instead of appending.
                    resume_from = 0
                mode = "ab" if resume_from > 0 else "wb"
                total = _response_total(response, resume_from, status)
                decryptor = (
                    AesCtrDecryptor(source.key, offset=resume_from)
                    if selection.transport == "encraw"
                    else None
                )

                done = resume_from
                await throttle.emit(done, total, force=True)
                handle = await asyncio.to_thread(part_path.open, mode)
                try:
                    async for chunk in response.content.iter_chunked(
                        self._chunk_bytes
                    ):
                        if not chunk:
                            continue
                        data = decryptor.update(chunk) if decryptor else chunk
                        if data:
                            await asyncio.to_thread(_write_chunk, handle, data)
                            done += len(data)
                        await throttle.emit(done, total)
                    if decryptor is not None:
                        tail = decryptor.finalize()
                        if tail:
                            await asyncio.to_thread(_write_chunk, handle, tail)
                            done += len(tail)
                finally:
                    await asyncio.to_thread(_flush_and_close, handle)

                if total is not None and done != total:
                    raise DownloadIncompleteError(done, total)
                await throttle.emit(done, total, force=True)
                return done, total
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise DownloadNetworkError(type(exc).__name__) from exc

    async def _ensure_session(self) -> Any:
        session = self._session
        if session is not None and not getattr(session, "closed", False):
            return session
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        self._owns_session = True
        return self._session

    # ------------------------------------------------------------------- cover

    async def _prepare_cover(self, source: SourceFile) -> bytes | None:
        url = source.cover_url
        if not url:
            return None
        if not self._cover_url_allowed(url):
            logger.warning("cover download skipped: host is not allowlisted")
            return None
        data = await self._fetch_cover(url)
        if data is None:
            return None
        try:
            return await asyncio.to_thread(self._render_cover, data)
        except Exception as exc:
            logger.warning("cover processing failed: %s", type(exc).__name__)
            return None

    def _cover_url_allowed(self, url: str) -> bool:
        try:
            split = urlsplit(url)
        except ValueError:
            return False
        if split.scheme not in {"http", "https"}:
            return False
        host = (split.hostname or "").lower().rstrip(".")
        return bool(host) and host in self._cover_hosts

    async def _fetch_cover(self, url: str) -> bytes | None:
        session = await self._ensure_session()
        try:
            # No redirects: a signed/allowlisted host must not bounce elsewhere.
            async with session.get(
                url, headers={"Accept": "image/*"}, allow_redirects=False
            ) as response:
                status = int(getattr(response, "status", 0))
                if status != 200:
                    logger.warning("cover fetch failed: HTTP %s", status)
                    return None
                length = _header_int(response.headers, "Content-Length")
                if length is not None and length > COVER_MAX_BYTES:
                    logger.warning("cover fetch skipped: response is too large")
                    return None
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(self._chunk_bytes):
                    buffer.extend(chunk)
                    if len(buffer) > COVER_MAX_BYTES:
                        logger.warning("cover fetch skipped: response is too large")
                        return None
                return bytes(buffer) if buffer else None
        except Exception as exc:
            logger.warning("cover fetch failed: %s", type(exc).__name__)
            return None

    def _render_cover(self, data: bytes) -> bytes:
        configured = int(self._settings.downloads.cover_size)
        size = max(64, min(configured, 4000))
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image = image.convert("RGB")
            fitted = ImageOps.fit(
                image, (size, size), method=Image.Resampling.LANCZOS
            )
            output = io.BytesIO()
            fitted.save(output, format="JPEG", quality=88, optimize=True)
            return output.getvalue()

    def _log_tag_report(self, task: DownloadTask, report: TagWriteReport) -> None:
        if report.warning:
            logger.warning(
                "tags for track %s (%s) not written: %s",
                task.id,
                report.container,
                report.warning,
            )


# --------------------------------------------------------------------- helpers


def _keep_partial(exc: BaseException) -> bool:
    return isinstance(exc, DownloadError) and exc.keep_partial


def _header_int(headers: Any, name: str) -> int | None:
    if headers is None:
        return None
    try:
        raw = headers.get(name)
    except (AttributeError, TypeError, ValueError):
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    return int(text) if text.isdigit() else None


def _content_range_start(headers: Any) -> int | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Content-Range")
    except (AttributeError, TypeError, ValueError):
        return None
    if not raw:
        return None
    prefix, _, _ = str(raw).partition("/")
    start_text = prefix.strip().removeprefix("bytes ").split("-", 1)[0].strip()
    return int(start_text) if start_text.isdigit() else None


def _response_total(response: Any, resume_from: int, status: int) -> int | None:
    headers = getattr(response, "headers", None)
    if status == 206:
        content_range = headers.get("Content-Range") if headers is not None else None
        if content_range:
            _, _, tail = str(content_range).partition("/")
            tail = tail.strip()
            if tail.isdigit():
                return int(tail)
        length = _header_int(headers, "Content-Length")
        return resume_from + length if length is not None else None
    if status == 200:
        return _header_int(headers, "Content-Length")
    return None


def _write_chunk(handle: Any, data: bytes) -> None:
    handle.write(data)


def _flush_and_close(handle: Any) -> None:
    try:
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()


def _replace_atomic(part_path: Path, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part_path, final_path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _unique_path(path: Path) -> Path:
    """Avoid clobbering another track that renders to the same template path."""

    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise DownloadError(f"cannot find a free target name for {path.name}")


def _write_cover_file(target: Path, data: bytes) -> None:
    if target.is_file() and target.stat().st_size > 0:
        return
    temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, target)
