"""Yandex Music REST endpoints (P3 contract: docs/API.md §6).

Read endpoints (catalog, wave) need no PIN; a missing/invalid Yandex token is
reported by the adapter as ``401 auth_required``. The adapter is resolved from
``app.state.ctx.yandex_adapter`` with a fallback to ``ctx.yandex`` so tests can
inject either name.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.core.models import (
    LibraryPage,
    WaveState,
    YandexAlbumDetail,
    YandexArtistDetail,
    YandexPlaylist,
    YandexPlaylistPage,
    YandexSearchResults,
)
from app.errors import AppError, UnavailableError
from app.integrations.yandex_music.mapper import COVER_HOST, COVER_SIZES, cover_path
from app.logging_setup import get_logger

logger = get_logger("api.yandex")

router = APIRouter(prefix="/yandex", tags=["yandex"])

COVER_CACHE_DIR = "yandex_covers"
COVER_MAX_BYTES = 8 * 1024 * 1024
# Process-local URI -> CoverCache key aliases. A miss simply refetches once.
_COVER_KEYS: dict[str, str] = {}

SearchTypeParam = Literal["all", "tracks", "albums", "artists", "playlists"]
WaveActionParam = Literal["like", "dislike", "skip"]


class PlaylistResponse(BaseModel):
    playlist: YandexPlaylist
    tracks: LibraryPage


class PlaylistCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    visibility: Literal["private", "public"] = "private"


class PlaylistRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class PlaylistTrackRequest(BaseModel):
    track_id: int = Field(ge=1)
    at: int = Field(default=0, ge=0)


class WaveFeedbackRequest(BaseModel):
    action: WaveActionParam
    track_id: int = Field(ge=1)
    played_seconds: float = Field(default=0, ge=0)


def _adapter(request: Request) -> Any:
    """Resolve the adapter; a P0/P1 mock without catalog methods is not ready."""

    context = get_context(request)
    adapter = getattr(context, "yandex_adapter", None)
    if adapter is None:
        adapter = getattr(context, "yandex", None)
    if adapter is None or not callable(getattr(adapter, "likes", None)):
        raise UnavailableError(
            "Yandex Music integration is not configured",
            code="yandex_not_configured",
            details={"hint": "set app.state.ctx.yandex_adapter to a YandexAdapter"},
        )
    return adapter


# ------------------------------------------------------------------- catalog


@router.get("/likes", response_model=LibraryPage)
async def likes(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LibraryPage:
    return await _adapter(request).likes(limit=limit, offset=offset)


@router.get("/playlists", response_model=YandexPlaylistPage)
async def playlists(request: Request) -> YandexPlaylistPage:
    return await _adapter(request).playlists()


@router.get("/playlists/{playlist_id}", response_model=PlaylistResponse)
async def playlist(
    request: Request,
    playlist_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlaylistResponse:
    dto, tracks = await _adapter(request).playlist(
        playlist_id, limit=limit, offset=offset
    )
    return PlaylistResponse(playlist=dto, tracks=tracks)


def _editing_adapter(request: Request) -> Any:
    adapter = _adapter(request)
    if not callable(getattr(adapter, "create_playlist", None)):
        raise UnavailableError(
            "Yandex playlist editing requires the real adapter",
            code="yandex_playlist_editing_unavailable",
        )
    return adapter


@router.post(
    "/playlists",
    response_model=YandexPlaylist,
    status_code=201,
    dependencies=[Depends(require_mutation_access)],
)
async def create_playlist(
    request: Request, payload: PlaylistCreateRequest
) -> YandexPlaylist:
    return await _editing_adapter(request).create_playlist(
        payload.title, visibility=payload.visibility
    )


@router.patch(
    "/playlists/{playlist_id}",
    response_model=YandexPlaylist,
    dependencies=[Depends(require_mutation_access)],
)
async def rename_playlist(
    request: Request, playlist_id: str, payload: PlaylistRenameRequest
) -> YandexPlaylist:
    return await _editing_adapter(request).rename_playlist(
        playlist_id, payload.title
    )


@router.delete(
    "/playlists/{playlist_id}",
    status_code=204,
    response_model=None,
    dependencies=[Depends(require_mutation_access)],
)
async def delete_playlist(request: Request, playlist_id: str) -> None:
    await _editing_adapter(request).delete_playlist(playlist_id)


@router.post(
    "/playlists/{playlist_id}/tracks",
    response_model=YandexPlaylist,
    dependencies=[Depends(require_mutation_access)],
)
async def add_playlist_track(
    request: Request, playlist_id: str, payload: PlaylistTrackRequest
) -> YandexPlaylist:
    return await _editing_adapter(request).add_playlist_track(
        playlist_id, payload.track_id, at=payload.at
    )


@router.delete(
    "/playlists/{playlist_id}/tracks",
    response_model=YandexPlaylist,
    dependencies=[Depends(require_mutation_access)],
)
async def remove_playlist_track(
    request: Request,
    playlist_id: str,
    position: Annotated[int, Query(ge=0)],
) -> YandexPlaylist:
    return await _editing_adapter(request).remove_playlist_track(
        playlist_id, position
    )


@router.get("/album/{album_id}", response_model=YandexAlbumDetail)
async def album(request: Request, album_id: str) -> YandexAlbumDetail:
    return await _adapter(request).album(album_id)


@router.get("/artist/{artist_id}", response_model=YandexArtistDetail)
async def artist(request: Request, artist_id: str) -> YandexArtistDetail:
    return await _adapter(request).artist(artist_id)


@router.get("/artist/{artist_id}/tracks", response_model=LibraryPage)
async def artist_tracks(
    request: Request,
    artist_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LibraryPage:
    adapter = _adapter(request)
    method = getattr(adapter, "artist_tracks", None)
    if not callable(method):
        raise UnavailableError(
            "Yandex artist pagination is unavailable",
            code="yandex_artist_pagination_unavailable",
        )
    return await method(artist_id, limit=limit, offset=offset)


@router.get("/search", response_model=YandexSearchResults)
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    type: Annotated[SearchTypeParam, Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> YandexSearchResults:
    return await _adapter(request).search(q, type_=type, limit=limit, offset=offset)


# ---------------------------------------------------------------------- wave


@router.post("/wave/start", response_model=WaveState)
async def wave_start(request: Request) -> WaveState:
    return await _adapter(request).wave_start()


@router.post("/wave/feedback", response_model=WaveState)
async def wave_feedback(request: Request, payload: WaveFeedbackRequest) -> WaveState:
    return await _adapter(request).wave_feedback(
        action=payload.action,
        track_id=payload.track_id,
        played_seconds=payload.played_seconds,
    )


# --------------------------------------------------------------- cover proxy


def _cover_request_path(uri: str, size: int) -> str:
    """Validate the requested cover and return a safe path on the CDN."""

    raw = uri.strip()
    if "://" in raw:
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() != COVER_HOST:
            raise AppError(
                "cover host is not allowed",
                code="cover_forbidden",
                status_code=403,
                details={"host": COVER_HOST},
            )
        candidate = f"{COVER_HOST}{parts.path}"
    else:
        candidate = raw
    path = cover_path(candidate, size)
    if path is None:
        raise AppError(
            "invalid cover uri",
            code="invalid_cover_uri",
            status_code=422,
        )
    return path


def _cache_file(directory: Any, key: str) -> Any:
    for suffix in (".jpg", ".png", ".webp"):
        candidate = directory / f"{key}{suffix}"
        if candidate.is_file():
            return candidate
    return None


async def _download_cover(url: str, directory: Any, key: str, timeout_s: float) -> Any:
    """Fetch one cover and cache it atomically; returns the cached path."""

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {"User-Agent": "MusicNX"}
    async with (
        aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
        session.get(url, allow_redirects=False) as response,
    ):
        if response.status != 200:
            raise UnavailableError(
                "Yandex cover is unavailable",
                code="cover_unavailable",
                details={"status": response.status},
            )
        content_type = response.headers.get("Content-Type", "image/jpeg")
        if not content_type.startswith("image/"):
            raise UnavailableError(
                "Yandex cover response is not an image",
                code="cover_unavailable",
            )
        data = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            data.extend(chunk)
            if len(data) > COVER_MAX_BYTES:
                raise UnavailableError(
                    "Yandex cover is too large",
                    code="cover_unavailable",
                )
    suffix = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
    if suffix == ".jpe":
        suffix = ".jpg"
    target = directory / f"{key}{suffix}"
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    await asyncio.to_thread(temporary.write_bytes, bytes(data))
    await asyncio.to_thread(os.replace, temporary, target)
    return target


async def _download_cover_bytes(url: str, timeout_s: float) -> bytes:
    """Fetch one image into memory (bounded); caller stores it in CoverCache."""

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {"User-Agent": "MusicNX"}
    async with (
        aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
        session.get(url, allow_redirects=False) as response,
    ):
        if response.status != 200:
            raise UnavailableError(
                "Yandex cover is unavailable",
                code="cover_unavailable",
                details={"status": response.status},
            )
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise UnavailableError(
                "Yandex cover response is not an image",
                code="cover_unavailable",
            )
        data = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            data.extend(chunk)
            if len(data) > COVER_MAX_BYTES:
                raise UnavailableError(
                    "Yandex cover is too large",
                    code="cover_unavailable",
                )
    return bytes(data)


@router.get("/cover")
async def cover(
    request: Request,
    uri: Annotated[str, Query(min_length=1, max_length=2048)],
    size: Annotated[int, Query()] = 300,
) -> Any:
    if size not in COVER_SIZES:
        raise AppError(
            "unsupported cover size",
            code="invalid_cover_size",
            status_code=422,
            details={"allowed": list(COVER_SIZES)},
        )
    context = get_context(request)
    path = _cover_request_path(uri, size)
    cache_key = hashlib.sha256(f"{size}:{path}".encode()).hexdigest()

    # P4: real covers go through the same CoverCache/LRU as local artwork and
    # are served by /artwork; the legacy file cache stays as a test fallback.
    covers = getattr(context, "covers", None)
    if covers is not None and size in getattr(covers, "sizes", ()):
        key = _COVER_KEYS.get(cache_key)
        if key is not None and covers.path_for(key, size).is_file():
            return RedirectResponse(
                f"/api/v1/artwork/{key}?size={size}",
                status_code=307,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )
        data = await _download_cover_bytes(
            f"https://{COVER_HOST}{path}",
            context.settings.yandex.request_timeout_s,
        )
        key, evicted = await covers.store(data)
        library = getattr(context, "library_repo", None)
        if library is not None:
            for evicted_key in evicted:
                await library.clear_cover_key(evicted_key)
        if len(_COVER_KEYS) > 2048:
            _COVER_KEYS.clear()
        _COVER_KEYS[cache_key] = key
        return RedirectResponse(
            f"/api/v1/artwork/{key}?size={size}",
            status_code=307,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    directory = context.settings.paths.cache / COVER_CACHE_DIR
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    cached = _cache_file(directory, cache_key)
    if cached is None:
        cached = await _download_cover(
            f"https://{COVER_HOST}{path}",
            directory,
            cache_key,
            context.settings.yandex.request_timeout_s,
        )
    media_type = mimetypes.guess_type(cached.name)[0] or "image/jpeg"
    return FileResponse(
        cached,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
