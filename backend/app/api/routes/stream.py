"""``GET /stream/{track_id}``: local files, Yandex Range proxy and mock tone.

Nothing is transcoded and nothing is buffered in memory: local files are served
by Starlette's ``FileResponse`` (it implements byte ranges), Yandex tracks are
piped chunk by chunk from the CDN, and mock mode plays a generated tone cached on
disk. Tokens and signed URLs never appear in logs or responses.
"""

from __future__ import annotations

import asyncio
import math
import mimetypes
import os
import struct
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_context
from app.core.models import Track
from app.errors import NoPlayableSourceError, NotFoundError, UnavailableError
from app.logging_setup import get_logger

router = APIRouter(tags=["stream"])

logger = get_logger("api.stream")

CHUNK_SIZE = 64 * 1024
# A signed Yandex URL stops working with these statuses; resolve it once more.
STALE_LINK_STATUSES = frozenset({401, 403, 410})
MOCK_TONE_DIR = "mock_tone"
MOCK_TONE_MAX_SECONDS = 30
MOCK_TONE_RATE = 44_100
MOCK_TONE_FREQUENCY = 440.0


@router.get("/stream/{track_id}")
async def stream(track_id: int, request: Request) -> Any:
    context = get_context(request)
    track = await context.tracks.get(track_id)
    if track is None:
        raise NotFoundError(
            "track not found",
            code="track_not_found",
            details={"track_id": track_id},
        )

    media_path = await context.tracks.get_media_path(track_id)
    if media_path is not None and media_path.is_file():
        return _file_response(media_path)

    try:
        info = await _stream_info_for(context, track)
    except NoPlayableSourceError:
        raise _stream_unavailable(track, "no_online_source") from None
    url = info.url if hasattr(info, "url") else str(info)
    url = await _rewrite_cdn(context, url)
    if url.startswith("mock://"):
        tone = await _ensure_mock_tone(context.settings.paths.cache, track)
        return _file_response(tone)
    return await _proxy_stream(context, track, url, request, codec=info.codec)


# ------------------------------------------------------------------- local


def _file_response(path: Path) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- resolving


async def _stream_info_for(
    context: Any, track: Track, *, force_refresh: bool = False
) -> Any:
    streaming = getattr(context, "yandex_streaming", None)
    if streaming is not None and callable(getattr(streaming, "stream_info", None)):
        return await streaming.stream_info(track.id, force_refresh=force_refresh)

    adapter = getattr(context, "yandex_adapter", None)
    if adapter is None:
        adapter = getattr(context, "yandex", None)
    if adapter is None:
        raise _stream_unavailable(track, "yandex_not_configured")

    by_track = getattr(adapter, "stream_info_for_track", None)
    if callable(by_track):
        return await by_track(track.id)
    external_id = track.external_id
    if external_id is None:
        raise _stream_unavailable(track, "no_online_source")
    return await adapter.stream_info(external_id)


def _stream_unavailable(track: Track, reason: str) -> UnavailableError:
    return UnavailableError(
        "stream is unavailable",
        code="stream_unavailable",
        details={"track_id": track.id, "reason": reason},
    )


async def _rewrite_cdn(context: Any, url: str) -> str:
    cdn = getattr(context, "cdn", None)
    if cdn is None or not url.startswith("https://"):
        return url
    return await cdn.rewrite(url)


def _upstream_error(track: Track, exc: BaseException) -> UnavailableError:
    """Map a CDN failure to a stable reason the UI can explain.

    A connect timeout almost always means the redirect target
    (``ext-strm-*.strm.yandex.net``) is unreachable from the Pi's network — an
    ISP/VPN block, not a stale link. Saying so beats a generic 503.
    """

    timed_out = isinstance(exc, TimeoutError)
    reason = "cdn_timeout" if timed_out else type(exc).__name__
    message = (
        "Yandex CDN did not respond: the Pi cannot reach the stream host"
        if timed_out
        else "upstream stream is unavailable"
    )
    return UnavailableError(
        message,
        code="stream_unavailable",
        details={
            "track_id": track.id,
            "reason": reason,
            "hint": "check the Pi network/VPN; offline tracks still play",
        },
    )


# -------------------------------------------------------------------- proxy


async def _open_upstream(
    url: str, range_header: str | None
) -> tuple[aiohttp.ClientSession, aiohttp.ClientResponse]:
    """Open the upstream response; kept module-level so tests can stub it."""

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=60)
    session = aiohttp.ClientSession(timeout=timeout)
    headers = {"Range": range_header} if range_header else {}
    try:
        response = await session.get(url, headers=headers, allow_redirects=True)
    except BaseException:
        await session.close()
        raise
    return session, response


def _upstream_headers(
    response: aiohttp.ClientResponse, codec: str | None = None
) -> dict[str, str]:
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    content_type = response.headers.get("Content-Type")
    if not content_type or content_type.startswith("application/octet-stream"):
        # Yandex CDN sometimes labels signed MP3/AAC as octet-stream; browsers
        # need a real audio type for <audio> playback.
        content_type = {
            "mp3": "audio/mpeg",
            "aac": "audio/aac",
            "flac": "audio/flac",
        }.get((codec or "").lower(), content_type)
    if content_type:
        headers["Content-Type"] = content_type
    content_range = response.headers.get("Content-Range")
    if content_range:
        headers["Content-Range"] = content_range
    content_length = response.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length
    return headers


async def _body(
    response: aiohttp.ClientResponse, session: aiohttp.ClientSession
) -> AsyncIterator[bytes]:
    """Pipe bytes as they arrive and close the upstream on any exit path."""

    try:
        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
            if chunk:
                yield chunk
    finally:
        response.release()
        await session.close()


async def _proxy_stream(
    context: Any,
    track: Track,
    url: str,
    request: Request,
    *,
    codec: str | None = None,
) -> StreamingResponse:
    range_header = request.headers.get("range")
    try:
        session, response = await _open_upstream(url, range_header)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise _upstream_error(track, exc) from None
    if response.status in STALE_LINK_STATUSES:
        response.release()
        await session.close()
        refreshed = await _stream_info_for(context, track, force_refresh=True)
        refreshed_url = refreshed.url if hasattr(refreshed, "url") else str(refreshed)
        refreshed_url = await _rewrite_cdn(context, refreshed_url)
        try:
            session, response = await _open_upstream(
                refreshed_url, range_header
            )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise _upstream_error(track, exc) from None

    if response.status >= 400:
        status = response.status
        response.release()
        await session.close()
        raise UnavailableError(
            "upstream stream is unavailable",
            code="stream_unavailable",
            details={"track_id": track.id, "status": status},
        )

    status_code = 206 if response.status == 206 else 200
    return StreamingResponse(
        _body(response, session),
        status_code=status_code,
        headers=_upstream_headers(response, codec),
    )


# ----------------------------------------------------------------- mock tone


def _tone_seconds(track: Track) -> int:
    duration_ms = track.duration_ms or 30_000
    seconds = math.ceil(duration_ms / 5_000) * 5
    return max(10, min(MOCK_TONE_MAX_SECONDS, seconds))


def _write_tone(path: Path, seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    total_frames = seconds * MOCK_TONE_RATE
    fade_frames = max(MOCK_TONE_RATE // 50, 1)  # 20 ms fade in/out, no clicks
    step = 2 * math.pi * MOCK_TONE_FREQUENCY / MOCK_TONE_RATE
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(MOCK_TONE_RATE)
        buffer = bytearray()
        for index in range(total_frames):
            amplitude = 0.18
            if index < fade_frames:
                amplitude *= index / fade_frames
            elif index > total_frames - fade_frames:
                amplitude *= (total_frames - index) / fade_frames
            sample = int(amplitude * 32767 * math.sin(step * index))
            buffer += struct.pack("<h", sample)
        handle.writeframes(bytes(buffer))
    os.replace(temporary, path)


async def _ensure_mock_tone(cache_dir: Path, track: Track) -> Path:
    seconds = _tone_seconds(track)
    path = cache_dir / MOCK_TONE_DIR / f"tone-{seconds}.wav"
    if not path.is_file():
        await asyncio.to_thread(_write_tone, path, seconds)
    return path
