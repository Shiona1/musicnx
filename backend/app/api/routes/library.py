"""Library read endpoints, import control and local search (P2)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.config import resolved_import_roots
from app.core.models import (
    AlbumDetail,
    AlbumPage,
    ArtistDetail,
    ArtistPage,
    LibraryPage,
    SearchResults,
    Track,
)
from app.downloads.tags import TrackTags, write_tags
from app.errors import AppError, NotFoundError
from app.library.uploads import save_upload

router = APIRouter(prefix="/library", tags=["library"])

AvailabilityFilter = Literal[
    "all", "offline", "online", "metadata_only", "missing", "unavailable"
]


class ImportRequest(BaseModel):
    paths: list[str] | None = None


class TrackUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    artists: list[str] | None = Field(default=None, min_length=1)
    album: str | None = Field(default=None, min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=0, le=9999)
    genre: str | None = Field(default=None, max_length=200)
    track_number: int | None = Field(default=None, ge=0, le=9999)
    disc_number: int | None = Field(default=None, ge=0, le=9999)


@router.get("/tracks", response_model=LibraryPage)
async def list_tracks(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
    availability: Annotated[AvailabilityFilter, Query()] = "all",
) -> LibraryPage:
    context = get_context(request)
    items, total = await context.tracks.list(
        limit=limit,
        offset=offset,
        search=search,
        availability=availability,
    )
    return LibraryPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/albums", response_model=AlbumPage)
async def list_albums(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> AlbumPage:
    context = get_context(request)
    items, total = await context.library_repo.list_albums(
        limit=limit, offset=offset, search=search
    )
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/artists", response_model=ArtistPage)
async def list_artists(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> ArtistPage:
    context = get_context(request)
    items, total = await context.library_repo.list_artists(
        limit=limit, offset=offset, search=search
    )
    return ArtistPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
async def album_detail(request: Request, album_id: int) -> AlbumDetail:
    detail = await get_context(request).library_repo.album_detail(album_id)
    if detail is None:
        raise NotFoundError("album not found", details={"album_id": album_id})
    return detail


@router.get("/artists/{artist_id}", response_model=ArtistDetail)
async def artist_detail(request: Request, artist_id: int) -> ArtistDetail:
    detail = await get_context(request).library_repo.artist_detail(artist_id)
    if detail is None:
        raise NotFoundError("artist not found", details={"artist_id": artist_id})
    return detail


@router.get("/search", response_model=SearchResults)
async def search_library(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchResults:
    context = get_context(request)
    tracks, track_total = await context.tracks.list(
        limit=limit, offset=offset, search=q
    )
    albums, album_total = await context.library_repo.list_albums(
        limit=limit, offset=offset, search=q
    )
    artists, artist_total = await context.library_repo.list_artists(
        limit=limit, offset=offset, search=q
    )
    return SearchResults(
        tracks=LibraryPage(
            items=tracks, total=track_total, limit=limit, offset=offset
        ),
        albums=AlbumPage(
            items=albums, total=album_total, limit=limit, offset=offset
        ),
        artists=ArtistPage(
            items=artists, total=artist_total, limit=limit, offset=offset
        ),
    )


@router.delete(
    "/tracks/{track_id}",
    dependencies=[Depends(require_mutation_access)],
)
async def delete_track(
    request: Request,
    track_id: int,
    delete_file: bool = False,
) -> dict:
    """Remove a track from the library; files stay by default.

    ``delete_file=true`` is explicit and only removes a local file inside a
    configured import root when no other track references it.
    """

    context = get_context(request)
    roots = resolved_import_roots(context.settings)
    deleted, files = await context.library_repo.delete_track(
        track_id, delete_file=delete_file, roots=roots
    )
    if not deleted:
        raise NotFoundError("track not found", details={"track_id": track_id})
    return {"deleted": True, "soft_deleted": True, "files_deleted": files}


@router.post(
    "/tracks/{track_id}/restore",
    response_model=Track,
    dependencies=[Depends(require_mutation_access)],
)
async def restore_track(request: Request, track_id: int) -> Track:
    context = get_context(request)
    restored = await context.library_repo.restore_track(track_id)
    if not restored:
        raise NotFoundError("track is not tombstoned", details={"track_id": track_id})
    track = await context.tracks.get(track_id)
    if track is None:  # pragma: no cover - just restored
        raise NotFoundError("track not found", details={"track_id": track_id})
    return track


@router.patch(
    "/tracks/{track_id}",
    dependencies=[Depends(require_mutation_access)],
)
async def update_track(
    request: Request, track_id: int, payload: TrackUpdateRequest
) -> dict:
    """Edit library metadata and best-effort rewrite file tags."""

    context = get_context(request)
    if all(
        value is None
        for value in (
            payload.title,
            payload.artists,
            payload.album,
            payload.year,
            payload.genre,
            payload.track_number,
            payload.disc_number,
        )
    ):
        raise AppError("at least one field is required", code="validation_error", status_code=422)
    try:
        track = await context.library_repo.update_track_metadata(
            track_id,
            title=payload.title,
            artists=payload.artists,
            album=payload.album,
            year=payload.year,
            genre=payload.genre,
            track_number=payload.track_number,
            disc_number=payload.disc_number,
        )
    except ValueError as exc:
        raise AppError(str(exc), code="validation_error", status_code=422) from None
    if track is None:
        raise NotFoundError("track not found", details={"track_id": track_id})

    file_tag_written = "skipped"
    tag_warning: str | None = None
    media_path = await context.tracks.get_media_path(track_id)
    if media_path is not None:
        tags = TrackTags(
            title=track.title,
            artists=tuple(artist.name for artist in track.artists),
            album=track.album.title if track.album else None,
            year=payload.year or (track.album.year if track.album else None),
            genre=payload.genre if payload.genre is not None else track.genre,
            track_number=payload.track_number,
            disc_number=payload.disc_number,
        )
        try:
            report = await asyncio.to_thread(write_tags, media_path, tags)
            file_tag_written = "written" if report.written else "skipped"
            tag_warning = report.warning
        except Exception as exc:  # metadata update must not fail on tag writer
            file_tag_written = "failed"
            tag_warning = type(exc).__name__
    return {
        "track": track.model_dump(),
        "file_tags": file_tag_written,
        "warning": tag_warning,
    }


@router.post(
    "/upload",
    status_code=201,
    dependencies=[Depends(require_mutation_access)],
)
async def upload_track(
    request: Request,
    file: Annotated[UploadFile, File(description="audio file")],
) -> dict:
    """Save one browser upload under the first import root.

    The file is not imported here: the client may upload several files and then
    call ``POST /library/import`` with their paths (the importer is
    single-flight). The response path is already inside a configured root.
    """

    context = get_context(request)
    saved = await save_upload(file, context.settings)
    return {
        "file": {
            "name": saved.name,
            "size": saved.size,
            "path": str(saved.path),
        }
    }


@router.post(
    "/import",
    status_code=202,
    dependencies=[Depends(require_mutation_access)],
)
async def start_import(
    request: Request, payload: ImportRequest | None = None
) -> dict:
    context = get_context(request)
    job = await context.importer.start(payload.paths if payload else None)
    return job.as_dict()


@router.get("/import/status")
async def import_status(request: Request) -> dict:
    context = get_context(request)
    return {"job": context.importer.status()}


@router.post(
    "/import/cancel",
    dependencies=[Depends(require_mutation_access)],
)
async def cancel_import(request: Request) -> dict:
    context = get_context(request)
    job = await context.importer.cancel()
    return job.as_dict()
