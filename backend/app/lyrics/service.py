"""Lyrics orchestration: SQLite cache, local ``.lrc`` files and Yandex Music.

Resolution order for ``GET /api/v1/lyrics/{track_id}`` (docs/API.md §9):

1. the SQLite cache (:class:`~app.lyrics.repository.LyricsRepository`);
2. a ``.lrc`` file sitting next to the track's local media file;
3. Yandex Music ``tracks_lyrics`` — LRC first, plain TEXT as a fallback;
4. also, in mock mode, deterministic three-line sample lyrics (source ``mock``)
   so the karaoke UI can be developed without network access.

Everything freshly resolved is cached and announced on the event bus as
``lyrics.updated {track_id, available}``.  The service never logs lyrics text,
download URLs or tokens: only the internal track id, provider name and format
(AGENTS §8).

Parsing (CPU) and the local file read (blocking I/O) run in worker threads, the
Yandex call and the lyrics download are async and bounded by a timeout, so the
event loop is never blocked (AGENTS §6).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import aiohttp

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import LyricsDTO, Track
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.errors import AppError, ConflictError, NotFoundError
from app.integrations.yandex_music.base import YandexAdapter
from app.integrations.yandex_music.client import YandexAuthProvider
from app.logging_setup import get_logger
from app.lyrics import lrclib
from app.lyrics.parser import ParsedLyrics, parse
from app.lyrics.repository import (
    OFFSET_LIMIT_MS,
    LyricsRecord,
    LyricsRepository,
    clamp_offset,
    normalize_language,
)

logger = get_logger("lyrics.service")

#: UI offset bounds (shared with the repository/API validation).
OFFSET_MIN_MS = -OFFSET_LIMIT_MS
OFFSET_MAX_MS = OFFSET_LIMIT_MS

#: Whole Yandex attempt (metadata call + download) must finish within this.
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_HTTP_TIMEOUT_S = 10.0

#: A lyrics document is small; cap the download so a broken/hostile URL cannot
#: exhaust memory. Anything longer is truncated, the parser skips the tail.
_MAX_DOWNLOAD_BYTES = 512 * 1024

#: Yandex formats tried in order: timed LRC first, plain text as fallback.
_YANDEX_FORMATS: tuple[str, ...] = ("LRC", "TEXT")

#: Downloaded/local lyrics are stored as-is; the provider name describes origin.
SOURCE_LRC = "lrc"
SOURCE_YANDEX = "yandex"
SOURCE_LRCLIB = "lrclib"
SOURCE_MOCK = "mock"

HttpFetcher = Callable[[str], Awaitable[str]]
LrclibFetcher = Callable[
    [str, str, str | None, int | None], Awaitable[Mapping[str, str] | None]
]


async def _aiohttp_get(url: str) -> str:
    """Download a lyrics document over plain HTTP(S) with a hard timeout.

    The URL handed out by Yandex is already signed; the library's authenticated
    client is only used as a fallback (see ``_fetch_yandex_lyrics``), so no token
    ever travels through this module or its logs.
    """

    timeout = aiohttp.ClientTimeout(total=DEFAULT_HTTP_TIMEOUT_S)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(url) as response,
    ):
        if response.status != 200:
            raise AppError(
                "lyrics download failed",
                code="lyrics_download_failed",
                status_code=502,
            )
        payload = await response.content.read(_MAX_DOWNLOAD_BYTES)
    return payload.decode("utf-8", errors="replace")


def _download_url(lyrics: Any) -> str | None:
    """Extract a download URL from a library ``TrackLyrics`` (str or mapping).

    ``yandex_music`` 3.2.2 exposes ``download_url`` as ``str``; older schemas
    used a ``{"text": ..., "lrc": ...}`` mapping — both are accepted.
    """

    raw = getattr(lyrics, "download_url", None)
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, Mapping):
        for key in ("lrc", "text", "plain"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
        for value in raw.values():
            if isinstance(value, str) and value:
                return value
    return None


def _yandex_track_id(track: Track) -> str | None:
    """Yandex catalogue id (``"1234:5678"`` -> ``"1234"``) or ``None``."""

    if (track.source or "").lower() != SOURCE_YANDEX:
        return None
    external_id = (track.external_id or "").strip()
    head = external_id.split(":", 1)[0]
    return head if head.isdigit() else None


def _lrc_candidates(media_path: Path) -> list[Path]:
    """``.lrc`` files that may sit next to a media file (case variants)."""

    candidates = [media_path.with_suffix(".lrc"), media_path.with_suffix(".LRC")]
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _mock_lrc(track: Track) -> str:
    """Deterministic three-line LRC for mock mode (no network, source ``mock``)."""

    title = " ".join((track.title or "MusicNX").split())
    artist = " ".join((track.artists[0].name if track.artists else "MusicNX").split())
    return (
        "[00:00.00]MusicNX mock lyrics\n"
        f"[00:04.00]{title}\n"
        f"[00:09.00]{artist}\n"
    )


class LyricsService:
    """Single entry point for lyrics reads and the per-track offset."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        tracks: TrackRepository,
        yandex_adapter: YandexAdapter,
        yandex_auth: YandexAuthProvider,
        bus: EventBus,
        *,
        repository: LyricsRepository | None = None,
        http_fetch: HttpFetcher | None = None,
        lrclib_fetch: LrclibFetcher | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._tracks = tracks
        self._repository = repository or LyricsRepository(db)
        # The frozen adapter protocol has no lyrics method yet; the authenticated
        # library client is reached through the auth manager (see docs/API.md §9).
        self._yandex_adapter = yandex_adapter
        self._yandex_auth = yandex_auth
        self._bus = bus
        self._http_fetch = http_fetch or _aiohttp_get
        self._lrclib_fetch = lrclib_fetch or lrclib.lookup
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------ public

    async def get(self, track_id: int, language: str | None = None) -> LyricsDTO:
        """Return lyrics for a track; ``available=False`` when none exist."""

        track = await self._require_track(track_id)
        requested = None if language is None else normalize_language(language)

        record = await self._repository.get(track_id, requested)
        if record is not None:
            return await self._dto_from_record(record)

        resolved = await self._resolve(track)
        if resolved is None:
            await self._publish(track_id, available=False)
            return self._unavailable(track_id, requested or "und")

        parsed, content, source = resolved
        record = await self._repository.save(
            track_id,
            language=requested or "und",
            format=parsed.format,
            content=content,
            source=source,
        )
        await self._publish(track_id, available=True)
        logger.info(
            "lyrics cached: track_id=%s source=%s format=%s lines=%d",
            track_id,
            source,
            parsed.format,
            len(parsed.lines),
        )
        return await self._dto_from_record(record)

    async def set_offset(self, track_id: int, offset_ms: int) -> LyricsDTO:
        """Persist the karaoke offset (clamped to +/-60 s) and return the DTO."""

        await self._require_track(track_id)
        dto = await self.get(track_id)
        if not dto.available:
            raise ConflictError(
                "lyrics are not available for this track",
                code="lyrics_unavailable",
                details={"track_id": track_id},
            )
        clamped = clamp_offset(offset_ms)
        if not await self._repository.set_offset(track_id, clamped):
            # Raced with a concurrent lyrics deletion; same outcome for the client.
            raise ConflictError(
                "lyrics are not available for this track",
                code="lyrics_unavailable",
                details={"track_id": track_id},
            )
        return dto.model_copy(update={"offset_ms": clamped})

    # ----------------------------------------------------------------- helpers

    async def _require_track(self, track_id: int) -> Track:
        track = await self._tracks.get(track_id)
        if track is None:
            raise NotFoundError("Track not found", details={"track_id": track_id})
        return track

    async def _resolve(self, track: Track) -> tuple[ParsedLyrics, str, str] | None:
        """Find fresh lyrics: local ``.lrc`` -> mock mode -> Yandex Music."""

        local_text = await self._local_lrc_text(track.id)
        if local_text is not None:
            parsed = await asyncio.to_thread(parse, local_text)
            if parsed.lines or parsed.plain:
                return parsed, local_text, SOURCE_LRC

        if self._settings.mode == "mock":
            mock_text = _mock_lrc(track)
            parsed = await asyncio.to_thread(parse, mock_text, "lrc")
            return parsed, mock_text, SOURCE_MOCK

        resolved = await self._resolve_yandex(track)
        if resolved is not None:
            return resolved
        return await self._resolve_lrclib(track)

    async def _resolve_yandex(
        self, track: Track
    ) -> tuple[ParsedLyrics, str, str] | None:
        yandex_id = _yandex_track_id(track)
        if yandex_id is None:
            return None
        for format_ in _YANDEX_FORMATS:
            text = await self._try_fetch_yandex(track.id, yandex_id, format_)
            if text is None:
                continue
            parsed = await asyncio.to_thread(parse, text, format_.lower())
            if parsed.lines or parsed.plain:
                return parsed, text, SOURCE_YANDEX
        return None

    async def _resolve_lrclib(
        self, track: Track
    ) -> tuple[ParsedLyrics, str, str] | None:
        """Last resort: LRCLIB's community database (metadata only on the wire)."""

        if not self._settings.lyrics.lrclib_enabled:
            return None
        artist = track.artists[0].name if track.artists else ""
        if not artist.strip() or not track.title.strip():
            return None
        album = track.album.title if track.album is not None else None
        duration_s = (
            round(track.duration_ms / 1000)
            if track.duration_ms and track.duration_ms > 0
            else None
        )
        try:
            document = await asyncio.wait_for(
                self._lrclib_fetch(artist, track.title, album, duration_s),
                timeout=self._settings.lyrics.lrclib_timeout_s,
            )
        except Exception as exc:
            logger.warning(
                "lyrics fetch failed: track_id=%s provider=lrclib reason=%s",
                track.id,
                type(exc).__name__,
            )
            return None
        if not document:
            return None
        synced = document.get("syncedLyrics")
        plain = document.get("plainLyrics")
        if isinstance(synced, str) and synced.strip():
            text, format_ = synced, "lrc"
        elif isinstance(plain, str) and plain.strip():
            text, format_ = plain, "text"
        else:
            return None
        parsed = await asyncio.to_thread(parse, text, format_)
        if not parsed.lines and not parsed.plain:
            return None
        logger.info(
            "lyrics provider fallback: track_id=%s provider=lrclib format=%s",
            track.id,
            parsed.format,
        )
        return parsed, text, SOURCE_LRCLIB

    async def _try_fetch_yandex(
        self, track_id: int, yandex_id: str, format_: str
    ) -> str | None:
        try:
            return await self._fetch_yandex_lyrics(yandex_id, format_)
        except Exception as exc:
            # Upstream failures degrade to "no lyrics": a lyrics request is never
            # allowed to turn into a 500, and the reason never includes the URL.
            logger.warning(
                "lyrics fetch failed: track_id=%s provider=yandex format=%s reason=%s",
                track_id,
                format_.lower(),
                type(exc).__name__,
            )
            return None

    async def _fetch_yandex_lyrics(self, yandex_id: str, format_: str) -> str | None:
        """Ask the authenticated library client for lyrics and download them."""

        async with asyncio.timeout(self._timeout_s):
            client = await self._yandex_auth.get_client()
            lyrics = await client.tracks_lyrics(yandex_id, format_=format_)
            if lyrics is None:
                return None

            url = _download_url(lyrics)
            if url is not None:
                text = await self._http_fetch(url)
                if text.strip():
                    return text

            # Fallback: the library retrieves through its authenticated session.
            fetch = getattr(lyrics, "fetch_lyrics_async", None)
            if callable(fetch):
                text = await fetch()
                if isinstance(text, str) and text.strip():
                    return text
        return None

    async def _local_lrc_text(self, track_id: int) -> str | None:
        media_path = await self._tracks.get_media_path(track_id)
        if media_path is None:
            return None
        for candidate in _lrc_candidates(media_path):
            text = await self._read_text(candidate, track_id)
            if text is not None:
                return text
        return None

    async def _read_text(self, path: Path, track_id: int) -> str | None:
        try:
            if not await asyncio.to_thread(path.is_file):
                return None
            text = await asyncio.to_thread(
                path.read_text, encoding="utf-8", errors="replace"
            )
        except OSError:
            logger.warning("lyrics: local .lrc could not be read: track_id=%s", track_id)
            return None
        return text if text.strip() else None

    async def _dto_from_record(self, record: LyricsRecord) -> LyricsDTO:
        parsed = await asyncio.to_thread(parse, record.content, record.format)
        return LyricsDTO(
            track_id=record.track_id,
            available=True,
            format=parsed.format,
            language=record.language,
            source=record.source,
            offset_ms=record.offset_ms,
            lines=list(parsed.lines),
            plain=parsed.plain,
        )

    @staticmethod
    def _unavailable(track_id: int, language: str) -> LyricsDTO:
        return LyricsDTO(
            track_id=track_id,
            available=False,
            format="none",
            language=normalize_language(language),
            source=None,
            offset_ms=0,
            lines=[],
            plain=None,
        )

    async def _publish(self, track_id: int, *, available: bool) -> None:
        await self._bus.publish(
            "lyrics.updated", {"track_id": track_id, "available": available}
        )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "OFFSET_MAX_MS",
    "OFFSET_MIN_MS",
    "LyricsService",
]
