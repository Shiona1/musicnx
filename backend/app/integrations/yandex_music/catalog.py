"""Yandex Music catalog: normalize live data into SQLite and return core DTOs.

Pagination is server-side over the lists Yandex has already returned: paged
endpoints (search, playlist tracks, likes) are fetched page by page and then
sliced, so the API's ``offset`` never silently points at row zero of a fresh
response. Every returned track is an internal ``Track`` row (docs/API.md §6.2),
which is what the queue and player consume.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.models import (
    AlbumPage,
    AlbumSummary,
    ArtistPage,
    ArtistRef,
    ArtistSummary,
    LibraryPage,
    Track,
    YandexAlbumDetail,
    YandexArtistDetail,
    YandexPlaylist,
    YandexPlaylistPage,
    YandexSearchResults,
)
from app.errors import AppError, NotFoundError, UnavailableError
from app.integrations.yandex_music.client import YandexClient
from app.integrations.yandex_music.mapper import (
    NormalizedAlbum,
    NormalizedArtist,
    NormalizedPlaylist,
    NormalizedSearchResults,
    NormalizedTrack,
    album_tracks,
    map_album,
    map_playlist,
    map_search,
    map_track,
    map_tracks,
    track_short_ids,
)
from app.integrations.yandex_music.mapper import (
    parse_playlist_id as _parse_playlist_id,
)
from app.integrations.yandex_music.repository import YandexRepository
from app.logging_setup import get_logger

logger = get_logger("yandex.catalog")

LIKES_PLAYLIST_KEY = "likes"
LIKES_PLAYLIST_TITLE = "Мне нравится"
ARTIST_PAGE_SIZE = 50
SEARCH_MAX_EXTRA_PAGES = 2


def parse_playlist_id(playlist_id: str) -> tuple[str, int]:
    """Split ``"<uid>:<kind>"`` into owner and playlist kind."""

    try:
        return _parse_playlist_id(playlist_id)
    except ValueError as exc:
        raise AppError(
            'playlist_id must be "<uid>:<kind>"',
            code="invalid_playlist_id",
            status_code=400,
            details={"playlist_id": playlist_id[:64]},
        ) from exc


def _page(items: Sequence[Any], offset: int, limit: int) -> list[Any]:
    return list(items[offset : offset + limit])


class YandexCatalogService:
    """Catalog reads for likes, playlists, albums, artists and search."""

    def __init__(self, client: YandexClient, repository: YandexRepository) -> None:
        self._client = client
        self._repository = repository

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _ordered_norms(
        raw_tracks: Sequence[Any], page_ids: Sequence[str]
    ) -> list[NormalizedTrack]:
        by_yandex_id: dict[str, NormalizedTrack] = {}
        for raw in raw_tracks:
            normalized = map_track(raw)
            if normalized is not None:
                by_yandex_id[normalized.yandex_id] = normalized
        return [by_yandex_id[track_id] for track_id in page_ids if track_id in by_yandex_id]

    async def _upsert_and_dtos(
        self,
        norms: Sequence[NormalizedTrack],
        *,
        liked_by_external: dict[str, bool] | None = None,
    ) -> list[Track]:
        """Upsert normalized tracks and rebuild DTOs with P3 provenance/cover."""

        if not norms:
            return []
        internal_ids = await self._repository.upsert_tracks(norms)
        tracks = await self._repository.list_tracks(internal_ids)
        by_external = {norm.external_id: norm for norm in norms}
        result: list[Track] = []
        for track in tracks:
            norm = by_external.get(track.external_id or "")
            update: dict[str, Any] = {}
            if norm is not None:
                update["source"] = "yandex"
                update["external_id"] = norm.external_id
                update["preview"] = norm.preview
                if norm.cover_url:
                    update["cover_url"] = norm.cover_url
            if liked_by_external and track.external_id in liked_by_external:
                update["liked"] = liked_by_external[track.external_id]
            result.append(track.model_copy(update=update) if update else track)
        return result

    async def _album_identity(
        self, album: NormalizedAlbum
    ) -> tuple[int, ArtistRef | None]:
        artist_ref: ArtistRef | None = None
        if album.artists:
            await self._repository.upsert_artists(album.artists)
            first = album.artists[0]
            artist_id = await self._repository.get_artist_id(first.external_id)
            if artist_id is not None:
                artist_ref = ArtistRef(id=artist_id, name=first.name)
        album_id = await self._repository.upsert_album(
            album, artist_id=artist_ref.id if artist_ref is not None else None
        )
        return album_id, artist_ref

    @staticmethod
    def _playlist_dto(
        playlist: NormalizedPlaylist, track_count: int | None = None
    ) -> YandexPlaylist:
        return YandexPlaylist(
            id=playlist.external_id,
            kind=playlist.kind,
            title=playlist.title,
            track_count=track_count if track_count is not None else playlist.track_count,
            cover_url=playlist.cover_url,
            owner=playlist.owner,
        )

    # ------------------------------------------------------------------ likes

    async def likes(self, *, limit: int, offset: int) -> LibraryPage:
        shorts = await self._client.likes()
        ids = track_short_ids(shorts)
        total = len(ids)
        page_ids = ids[offset : offset + limit]
        raw_tracks = await self._client.tracks(page_ids) if page_ids else []
        norms = self._ordered_norms(raw_tracks, page_ids)
        tracks = await self._upsert_and_dtos(norms)

        playlist_id = await self._repository.upsert_system_playlist(
            key=LIKES_PLAYLIST_KEY,
            title=LIKES_PLAYLIST_TITLE,
            kind="likes",
            track_count=total,
        )
        await self._repository.upsert_playlist_tracks(
            playlist_id,
            [(offset + index, track.id) for index, track in enumerate(tracks)],
        )
        items = [track.model_copy(update={"liked": True}) for track in tracks]
        return LibraryPage(items=items, total=total, limit=limit, offset=offset)

    # -------------------------------------------------------------- playlists

    async def playlists(self) -> YandexPlaylistPage:
        raw_playlists = await self._client.playlists()
        items: list[YandexPlaylist] = []
        for raw in raw_playlists:
            normalized = map_playlist(raw)
            if normalized is None:
                continue
            await self._repository.upsert_playlist(normalized)
            items.append(self._playlist_dto(normalized))
        return YandexPlaylistPage(items=items, total=len(items))

    async def playlist(
        self, playlist_id: str, *, limit: int, offset: int
    ) -> tuple[YandexPlaylist, LibraryPage]:
        uid, kind = parse_playlist_id(playlist_id)
        raw_playlist, shorts = await self._client.playlist_tracks(uid, kind)
        if raw_playlist is None:
            raise NotFoundError(
                "Yandex Music playlist not found",
                code="yandex_playlist_not_found",
                details={"playlist_id": playlist_id[:64]},
            )
        normalized = map_playlist(raw_playlist)
        if normalized is None:
            raise UnavailableError(
                "Yandex Music returned an unreadable playlist",
                code="yandex_schema_mismatch",
                details={"operation": "playlist"},
            )

        ids = track_short_ids(shorts)
        total = len(ids) or normalized.track_count
        page_ids = ids[offset : offset + limit]
        raw_tracks = await self._client.tracks(page_ids) if page_ids else []
        norms = self._ordered_norms(raw_tracks, page_ids)
        tracks = await self._upsert_and_dtos(norms)

        internal_playlist_id = await self._repository.upsert_playlist(normalized)
        await self._repository.upsert_playlist_tracks(
            internal_playlist_id,
            [(offset + index, track.id) for index, track in enumerate(tracks)],
        )
        return (
            self._playlist_dto(normalized, track_count=total),
            LibraryPage(items=tracks, total=total, limit=limit, offset=offset),
        )

    # ------------------------------------------------------------------ album

    async def _resolve_album_id(self, value: str) -> str:
        """Accept either an internal library id or a Yandex album id."""

        if value.isdigit():
            external = await self._repository.album_external_id(int(value))
            if external:
                return external
        return value

    async def _resolve_artist_id(self, value: str) -> str:
        """Accept either an internal library id or a Yandex artist id."""

        if value.isdigit():
            external = await self._repository.artist_external_id(int(value))
            if external:
                return external
        return value

    async def artist_tracks_page(
        self, artist_id: str, *, limit: int, offset: int
    ) -> LibraryPage:
        """Paginated artist tracks for the catalog UI (0-based offset)."""

        artist_id = await self._resolve_artist_id(artist_id)
        page_size = min(max(limit + offset, limit), 100)
        raw = await self._client.artist_tracks(
            artist_id, page=0, page_size=page_size
        )
        raw_tracks = list(getattr(raw, "tracks", None) or []) if raw is not None else []
        norms = map_tracks(raw_tracks)[offset : offset + limit]
        tracks = await self._upsert_and_dtos(norms)
        pager = getattr(raw, "pager", None)
        total = getattr(pager, "total", None)
        if total is None:
            total = len(raw_tracks)
        return LibraryPage(
            items=tracks, total=int(total), limit=limit, offset=offset
        )

    async def album(self, album_id: str) -> YandexAlbumDetail:
        album_id = await self._resolve_album_id(album_id)
        raw = await self._client.album(album_id)
        if raw is None:
            raise NotFoundError(
                "Yandex Music album not found",
                code="yandex_album_not_found",
                details={"album_id": str(album_id)[:64]},
            )
        normalized = map_album(raw)
        if normalized is None:
            raise UnavailableError(
                "Yandex Music returned an unreadable album",
                code="yandex_schema_mismatch",
                details={"operation": "album"},
            )
        tracks = await self._upsert_and_dtos(album_tracks(raw))
        internal_album_id, artist_ref = await self._album_identity(normalized)
        summary = AlbumSummary(
            id=internal_album_id,
            title=normalized.title,
            year=normalized.year,
            artist=artist_ref,
            cover_url=normalized.cover_url,
            track_count=len(tracks) or (normalized.track_count or 0),
        )
        return YandexAlbumDetail(album=summary, tracks=tracks)

    # ----------------------------------------------------------------- artist

    async def artist(self, artist_id: str) -> YandexArtistDetail:
        artist_id = await self._resolve_artist_id(artist_id)
        raw = await self._client.artist_tracks(
            artist_id, page=0, page_size=ARTIST_PAGE_SIZE
        )
        raw_tracks = list(getattr(raw, "tracks", None) or []) if raw is not None else []
        norms = map_tracks(raw_tracks)
        name = await self._repository.artist_name(str(artist_id))
        cover_url: str | None = None
        if name is None:
            for norm in norms:
                match = next(
                    (
                        artist
                        for artist in norm.artists
                        if artist.external_id == str(artist_id)
                    ),
                    None,
                )
                if match is not None:
                    name = match.name
                    cover_url = match.cover_url
                    break
        if name is None:
            # ``artists_tracks`` carries no artist object (YANDEX_API §2), so an
            # artist with no cached metadata degrades to the standard label.
            logger.warning(
                "artist %s has no cached name; using a generic label", artist_id
            )
            name = "Unknown Artist"

        await self._repository.upsert_artist(
            NormalizedArtist(external_id=str(artist_id), name=name, cover_url=cover_url)
        )
        tracks = await self._upsert_and_dtos(norms)
        internal_artist_id = await self._repository.get_artist_id(str(artist_id))
        if internal_artist_id is None:  # pragma: no cover - defensive
            raise UnavailableError(
                "Yandex Music artist could not be stored",
                code="yandex_schema_mismatch",
                details={"operation": "artist"},
            )
        summary = ArtistSummary(
            id=internal_artist_id,
            name=name,
            cover_url=cover_url,
            track_count=len(tracks),
            album_count=len({track.album.id for track in tracks if track.album}),
        )
        return YandexArtistDetail(artist=summary, tracks=tracks)

    # ----------------------------------------------------------------- search

    @staticmethod
    def _needs_more(norm: NormalizedSearchResults, offset: int, limit: int) -> bool:
        sections = (
            (len(norm.tracks), norm.track_total),
            (len(norm.albums), norm.album_total),
            (len(norm.artists), norm.artist_total),
            (len(norm.playlists), norm.playlist_total),
        )
        return any(
            total > have and offset + limit > have for have, total in sections
        )

    @staticmethod
    def _merge_search(
        first: NormalizedSearchResults, more: NormalizedSearchResults
    ) -> NormalizedSearchResults:
        def merged(items: tuple[Any, ...], extra: tuple[Any, ...]) -> tuple[Any, ...]:
            seen: set[str] = set()
            result: list[Any] = []
            for item in (*items, *extra):
                if item.external_id in seen:
                    continue
                seen.add(item.external_id)
                result.append(item)
            return tuple(result)

        return NormalizedSearchResults(
            tracks=merged(first.tracks, more.tracks),
            albums=merged(first.albums, more.albums),
            artists=merged(first.artists, more.artists),
            playlists=merged(first.playlists, more.playlists),
            track_total=first.track_total,
            album_total=first.album_total,
            artist_total=first.artist_total,
            playlist_total=first.playlist_total,
        )

    async def _album_summaries(
        self, albums: Sequence[NormalizedAlbum]
    ) -> list[AlbumSummary]:
        items: list[AlbumSummary] = []
        for album in albums:
            internal_id, artist_ref = await self._album_identity(album)
            items.append(
                AlbumSummary(
                    id=internal_id,
                    title=album.title,
                    year=album.year,
                    artist=artist_ref,
                    cover_url=album.cover_url,
                    track_count=album.track_count or 0,
                )
            )
        return items

    async def _artist_summaries(
        self, artists: Sequence[NormalizedArtist]
    ) -> list[ArtistSummary]:
        if not artists:
            return []
        internal_ids = await self._repository.upsert_artists(artists)
        return [
            ArtistSummary(
                id=internal_id,
                name=artist.name,
                cover_url=artist.cover_url,
                track_count=0,
                album_count=0,
            )
            for artist, internal_id in zip(artists, internal_ids, strict=True)
        ]

    async def _playlist_items(
        self, playlists: Sequence[NormalizedPlaylist]
    ) -> list[YandexPlaylist]:
        items: list[YandexPlaylist] = []
        for playlist in playlists:
            await self._repository.upsert_playlist(playlist)
            items.append(self._playlist_dto(playlist))
        return items

    async def search(
        self, query: str, *, type_: str, limit: int, offset: int
    ) -> YandexSearchResults:
        if not query.strip():
            raise AppError(
                "search query must not be empty",
                code="invalid_query",
                status_code=400,
            )
        raw = await self._client.search(query, type_=type_, page=0)
        norm = (
            map_search(raw) if raw is not None else NormalizedSearchResults()
        )
        pages = 0
        while pages < SEARCH_MAX_EXTRA_PAGES and self._needs_more(norm, offset, limit):
            pages += 1
            next_raw = await self._client.search(query, type_=type_, page=pages)
            if next_raw is None:
                break
            more = map_search(next_raw)
            if not (more.tracks or more.albums or more.artists or more.playlists):
                break
            norm = self._merge_search(norm, more)

        track_items = await self._upsert_and_dtos(_page(norm.tracks, offset, limit))
        album_items = await self._album_summaries(_page(norm.albums, offset, limit))
        artist_items = await self._artist_summaries(_page(norm.artists, offset, limit))
        playlist_items = await self._playlist_items(
            _page(norm.playlists, offset, limit)
        )
        return YandexSearchResults(
            tracks=LibraryPage(
                items=track_items,
                total=norm.track_total,
                limit=limit,
                offset=offset,
            ),
            albums=AlbumPage(
                items=album_items,
                total=norm.album_total,
                limit=limit,
                offset=offset,
            ),
            artists=ArtistPage(
                items=artist_items,
                total=norm.artist_total,
                limit=limit,
                offset=offset,
            ),
            playlists=YandexPlaylistPage(
                items=playlist_items, total=norm.playlist_total
            ),
        )
