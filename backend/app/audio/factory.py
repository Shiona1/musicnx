"""Choose an audio backend and playable-source resolver from settings."""

from __future__ import annotations

import os
from typing import Any, Protocol

import aiohttp

from app.audio.base import AudioBackend
from app.audio.mock_backend import MockAudioBackend
from app.audio.mpv_backend import MpvAudioBackend, WarningCallback, find_mpv
from app.config import Settings
from app.core.models import Track
from app.database.repositories import TrackRepository
from app.errors import ConfigurationError, NoPlayableSourceError, UnavailableError

STREAM_PROBE_TIMEOUT_S = 2.5


class PlayableSourceResolver(Protocol):
    async def resolve(self, track: Track) -> str: ...


class MockSourceResolver:
    """Virtual URIs for the mock catalog; the mock backend never opens them."""

    async def resolve(self, track: Track) -> str:
        duration = track.duration_ms or 0
        return f"mock://track/{track.id}?duration_ms={duration}"


class LibrarySourceResolver:
    """Resolve a local media file; Yandex streaming arrives in P3."""

    def __init__(self, tracks: TrackRepository) -> None:
        self._tracks = tracks

    async def resolve(self, track: Track) -> str:
        path = await self._tracks.get_media_path(track.id)
        if path is None:
            raise NoPlayableSourceError(
                "no local file or online source for this track",
                details={"track_id": track.id, "reason": "no_media_file"},
            )
        if not path.is_file():
            raise NoPlayableSourceError(
                "media file is missing",
                details={"track_id": track.id, "reason": "file_missing", "path": str(path)},
            )
        return str(path)


class OnlineSourceResolver:
    """Resolve a local file if present, otherwise a fresh Yandex stream URL."""

    def __init__(self, tracks: TrackRepository, yandex: Any, cdn: Any | None = None) -> None:
        self._tracks = tracks
        self._yandex = yandex
        self._cdn = cdn

    async def resolve(self, track: Track) -> str:
        path = await self._tracks.get_media_path(track.id)
        if path is not None and path.is_file():
            return str(path)
        if getattr(track, "source", None) == "yandex" and getattr(
            track, "external_id", None
        ):
            try:
                info = await self._yandex.stream_info_for_track(track.id)
            except AttributeError:
                info = await self._yandex.stream_info(track.external_id or "")
            url = info.url
            if self._cdn is not None:
                url = await self._cdn.rewrite(url)
            await _ensure_stream_reachable(url)
            return url
        if path is not None:
            raise NoPlayableSourceError(
                "media file is missing",
                details={"track_id": track.id, "reason": "file_missing", "path": str(path)},
            )
        raise NoPlayableSourceError(
            "no local file or online source for this track",
            details={"track_id": track.id, "reason": "no_media_file"},
        )


async def _ensure_stream_reachable(url: str) -> None:
    """Fail fast with a clear code when the CDN edge is unreachable.

    Yandex hands out a link on ``api.music.yandex.net`` that redirects to a
    regional ``ext-strm-*.strm.yandex.net`` edge. When that edge is blackholed
    by the provider/VPN, mpv would sit for its full 10 s load timeout; a short
    probe turns that into a 503 the UI can explain immediately.
    """

    if not url.startswith("https://"):
        return
    timeout = aiohttp.ClientTimeout(
        total=STREAM_PROBE_TIMEOUT_S, sock_connect=STREAM_PROBE_TIMEOUT_S
    )
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                url,
                headers={"Range": "bytes=0-0"},
                allow_redirects=True,
            ) as response,
        ):
            if response.status >= 400:
                raise UnavailableError(
                    "Yandex CDN rejected the stream",
                    code="cdn_unreachable",
                    details={"status": response.status},
                )
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise UnavailableError(
            "Yandex CDN is unreachable from the Pi",
            code="cdn_unreachable",
            details={"reason": type(exc).__name__},
        ) from None


def create_audio_backend(
    settings: Settings, on_warning: WarningCallback | None = None
) -> AudioBackend:
    """Build the configured backend; fail loudly on unsupported combinations."""

    if settings.mode == "mock" or settings.audio.backend == "mock":
        return MockAudioBackend()

    if os.name != "posix":
        raise ConfigurationError(
            "real mpv mode is supported only on POSIX (unix socket IPC); "
            "use mode=mock on Windows (ADR-006)"
        )

    if settings.audio.backend == "auto" and find_mpv(settings.audio.mpv_path) is None:
        from app.logging_setup import get_logger

        get_logger("audio.factory").warning(
            "mpv not found (%s); falling back to the mock audio backend",
            settings.audio.mpv_path,
        )
        return MockAudioBackend()

    return MpvAudioBackend(settings.audio, on_warning=on_warning)


def create_source_resolver(
    settings: Settings,
    tracks: TrackRepository,
    yandex_adapter: Any | None = None,
    cdn: Any | None = None,
) -> PlayableSourceResolver:
    if settings.mode == "mock" or settings.audio.backend == "mock":
        return MockSourceResolver()
    if yandex_adapter is not None:
        return OnlineSourceResolver(tracks, yandex_adapter, cdn)
    return LibrarySourceResolver(tracks)


def describe_audio_backend(backend: AudioBackend) -> dict[str, Any]:
    return backend.describe()
