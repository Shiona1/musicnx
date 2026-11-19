"""Deterministic mock Yandex Music adapter (no network, no token).

Mock mode must exercise the same code paths as real mode: every endpoint returns
internal SQLite tracks (upserted through :class:`YandexRepository`), so the queue,
player and ``/stream`` work without a single byte leaving the machine. The
canned catalog mirrors the shared P3 fixtures (``tests/fixtures/yandex/*.json``)
so UI, contract tests and e2e see one coherent music library.

Authentication is not re-implemented when an auth manager is supplied (flow A's
``MockYandexAuthManager``); a small in-memory fallback keeps the adapter usable
standalone in unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.event_bus import EventBus
from app.core.models import (
    AlbumPage,
    AlbumSummary,
    ArtistPage,
    ArtistRef,
    ArtistSummary,
    LibraryPage,
    Track,
    WaveState,
    YandexAlbumDetail,
    YandexArtistDetail,
    YandexAuthStatus,
    YandexDeviceStart,
    YandexDeviceStatus,
    YandexPlaylist,
    YandexPlaylistPage,
    YandexSearchResults,
)
from app.core.timeutils import utc_now_iso
from app.errors import AppError, AuthenticationError, NotFoundError
from app.integrations.yandex_music.base import (
    SearchType,
    WaveFeedback,
    YandexStreamInfo,
)
from app.integrations.yandex_music.client import call_auth
from app.integrations.yandex_music.mapper import (
    NormalizedAlbum,
    NormalizedArtist,
    NormalizedPlaylist,
    NormalizedTrack,
    parse_playlist_id,
)
from app.integrations.yandex_music.repository import YandexRepository
from app.logging_setup import get_logger

logger = get_logger("yandex.mock")

MOCK_UID = "1001"
MOCK_OWNER = "musicnx-demo"
WAVE_STATION = "user:onyourwave"
MIN_WAVE_TRACKS = 3
LIKES_PLAYLIST_TITLE = "Мне нравится"

_COVER_ALBUM_501 = (
    "https://avatars.yandex.net/get-music-content/9876543210/3f9c1a2b4d5e-300x300"
)
_COVER_ALBUM_502 = (
    "https://avatars.yandex.net/get-music-content/1234567890/9a8b7c6d5e4f-300x300"
)

_MOCK_ARTIST_ORCHESTRA = NormalizedArtist(
    external_id="301", name="Тестовый Оркестр", cover_url=_COVER_ALBUM_501
)
_MOCK_ARTIST_MOCK = NormalizedArtist(
    external_id="302", name="Mock Artist", cover_url=_COVER_ALBUM_502
)

_MOCK_ALBUM_MIDNIGHT = NormalizedAlbum(
    external_id="501",
    title="Midnight Sessions",
    year=2024,
    cover_url=_COVER_ALBUM_501,
    track_count=2,
    artists=(_MOCK_ARTIST_ORCHESTRA,),
)
_MOCK_ALBUM_CITIES = NormalizedAlbum(
    external_id="502",
    title="Cities",
    year=2023,
    cover_url=_COVER_ALBUM_502,
    track_count=2,
    artists=(_MOCK_ARTIST_MOCK,),
)


def _track(
    track_id: str,
    title: str,
    artist: NormalizedArtist,
    album: NormalizedAlbum,
    duration_ms: int,
    *,
    preview: bool = False,
) -> NormalizedTrack:
    return NormalizedTrack(
        external_id=f"{track_id}:{album.external_id}",
        yandex_id=track_id,
        album_id=album.external_id,
        title=title,
        artists=(artist,),
        album=album,
        duration_ms=duration_ms,
        year=album.year,
        available=True,
        preview=preview,
        cover_url=album.cover_url,
    )


MOCK_TRACKS: dict[str, NormalizedTrack] = {
    "9001": _track(
        "9001", "Полночный эфир", _MOCK_ARTIST_ORCHESTRA, _MOCK_ALBUM_MIDNIGHT, 214_000
    ),
    "9002": _track(
        "9002", "Северное сияние", _MOCK_ARTIST_ORCHESTRA, _MOCK_ALBUM_MIDNIGHT, 198_000
    ),
    "9003": _track(
        "9003", "Городские огни", _MOCK_ARTIST_MOCK, _MOCK_ALBUM_CITIES, 245_000
    ),
    "9004": _track(
        "9004",
        "Тихий дождь",
        _MOCK_ARTIST_MOCK,
        _MOCK_ALBUM_CITIES,
        176_000,
        preview=True,
    ),
}
ALL_TRACK_IDS: tuple[str, ...] = tuple(MOCK_TRACKS)

MOCK_ALBUMS: dict[str, NormalizedAlbum] = {
    _MOCK_ALBUM_MIDNIGHT.external_id: _MOCK_ALBUM_MIDNIGHT,
    _MOCK_ALBUM_CITIES.external_id: _MOCK_ALBUM_CITIES,
}
MOCK_ALBUM_TRACKS: dict[str, tuple[str, ...]] = {
    "501": ("9001", "9002"),
    "502": ("9003", "9004"),
}
MOCK_ARTISTS: dict[str, NormalizedArtist] = {
    _MOCK_ARTIST_ORCHESTRA.external_id: _MOCK_ARTIST_ORCHESTRA,
    _MOCK_ARTIST_MOCK.external_id: _MOCK_ARTIST_MOCK,
}
MOCK_ARTIST_TRACKS: dict[str, tuple[str, ...]] = {
    "301": ("9001", "9002"),
    "302": ("9003", "9004"),
}
MOCK_LIKES: tuple[str, ...] = ("9001", "9002", "9003", "9004")
MOCK_WAVE_START: tuple[str, ...] = ("9003", "9001", "9004")
MOCK_BATCH_ID = "wave-20261114T093000Z-0001"


@dataclass(frozen=True)
class _MockPlaylist:
    uid: str
    kind: int
    title: str
    track_ids: tuple[str, ...]
    cover_url: str | None = None
    owner: str = MOCK_OWNER

    @property
    def external_id(self) -> str:
        return f"{self.uid}:{self.kind}"


MOCK_PLAYLISTS: dict[int, _MockPlaylist] = {
    7: _MockPlaylist(
        uid=MOCK_UID,
        kind=7,
        title="Любимая подборка",
        track_ids=("9001", "9002", "9003", "9004"),
        cover_url=_COVER_ALBUM_501,
    )
}


def _playlist_as_normalized(playlist: _MockPlaylist) -> NormalizedPlaylist:
    return NormalizedPlaylist(
        external_id=playlist.external_id,
        uid=playlist.uid,
        kind=playlist.kind,
        title=playlist.title,
        track_count=len(playlist.track_ids),
        cover_url=playlist.cover_url,
        owner=playlist.owner,
    )


@dataclass
class _DeviceFlow:
    flow_id: str
    user_code: str
    expires_in: int
    interval: int
    polls: int = field(default=0)


class MockYandexAdapter:
    """In-memory implementation of :class:`YandexAdapter` for ``mode=mock``."""

    mode = "mock"
    backend = "mock"

    def __init__(
        self,
        repository: YandexRepository,
        *,
        bus: EventBus | None = None,
        auth: Any | None = None,
    ) -> None:
        self._repository = repository
        self._bus = bus
        self._auth_manager = auth
        self._auth_state = YandexAuthStatus(authenticated=False)
        self._flows: dict[str, _DeviceFlow] = {}
        self._liked: set[int] = {int(track_id) for track_id in MOCK_LIKES}
        self._wave_ids: list[str] = list(MOCK_WAVE_START)
        self._wave_index = 0
        self._batch_id: str | None = MOCK_BATCH_ID
        self._session_id: str | None = None

    # ------------------------------------------------------------------- auth

    async def auth_status(self) -> YandexAuthStatus:
        if self._auth_manager is not None:
            return await call_auth(self._auth_manager, "auth_status", "status")
        return self._auth_state.model_copy()

    async def start_device_auth(self) -> YandexDeviceStart:
        if self._auth_manager is not None:
            return await call_auth(
                self._auth_manager, "start_device_auth", "start_device_flow"
            )
        flow_id = f"mock-flow-{len(self._flows) + 1}"
        self._flows[flow_id] = _DeviceFlow(
            flow_id=flow_id, user_code="MOCK-CODE", expires_in=300, interval=5
        )
        return YandexDeviceStart(
            flow_id=flow_id,
            user_code="MOCK-CODE",
            verification_url="https://ya.ru/device",
            expires_in=300,
            interval=5,
            qr_data_uri=None,
        )

    async def device_auth_status(self, flow_id: str) -> YandexDeviceStatus:
        if self._auth_manager is not None:
            return await call_auth(
                self._auth_manager,
                "device_auth_status",
                "device_status",
                flow_id=flow_id,
            )
        flow = self._flows.get(flow_id)
        if flow is None:
            return YandexDeviceStatus(
                state="error", flow_id=flow_id, error="unknown_flow"
            )
        flow.polls += 1
        if flow.polls < 2:
            return YandexDeviceStatus(state="pending", flow_id=flow_id)
        self._auth_state = YandexAuthStatus(
            authenticated=True, login=MOCK_OWNER, source="device"
        )
        return YandexDeviceStatus(
            state="authorized", flow_id=flow_id, login=MOCK_OWNER
        )

    async def set_manual_token(self, token: str) -> YandexAuthStatus:
        if self._auth_manager is not None:
            return await call_auth(
                self._auth_manager, "set_manual_token", token=token
            )
        if not token or not token.strip():
            raise AuthenticationError(
                "invalid token", details={"reason": "invalid_token"}
            )
        self._auth_state = YandexAuthStatus(
            authenticated=True, login=MOCK_OWNER, source="manual"
        )
        return self._auth_state.model_copy()

    async def logout(self) -> None:
        if self._auth_manager is not None:
            await call_auth(self._auth_manager, "logout")
            return
        self._auth_state = YandexAuthStatus(authenticated=False)

    # ---------------------------------------------------------------- catalog

    async def likes(self, *, limit: int, offset: int) -> LibraryPage:
        norms = [MOCK_TRACKS[track_id] for track_id in MOCK_LIKES]
        tracks = await self._dtos(norms)
        items = [track.model_copy(update={"liked": True}) for track in tracks]
        return LibraryPage(
            items=items[offset : offset + limit],
            total=len(items),
            limit=limit,
            offset=offset,
        )

    async def playlists(self) -> YandexPlaylistPage:
        items = [
            self._playlist_dto(_playlist_as_normalized(playlist))
            for playlist in MOCK_PLAYLISTS.values()
        ]
        return YandexPlaylistPage(items=items, total=len(items))

    async def playlist(
        self, playlist_id: str, *, limit: int, offset: int
    ) -> tuple[YandexPlaylist, LibraryPage]:
        uid, kind = self._playlist_ref(playlist_id)
        playlist = MOCK_PLAYLISTS.get(kind)
        if uid != MOCK_UID or playlist is None:
            raise NotFoundError(
                "mock playlist not found",
                code="yandex_playlist_not_found",
                details={"playlist_id": playlist_id[:64]},
            )
        norms = [MOCK_TRACKS[track_id] for track_id in playlist.track_ids]
        tracks = await self._dtos(norms)
        return (
            self._playlist_dto(_playlist_as_normalized(playlist)),
            LibraryPage(
                items=tracks[offset : offset + limit],
                total=len(tracks),
                limit=limit,
                offset=offset,
            ),
        )

    async def album(self, album_id: str) -> YandexAlbumDetail:
        album = MOCK_ALBUMS.get(str(album_id))
        if album is None:
            raise NotFoundError(
                "mock album not found",
                code="yandex_album_not_found",
                details={"album_id": str(album_id)[:64]},
            )
        norms = [MOCK_TRACKS[track_id] for track_id in MOCK_ALBUM_TRACKS[album.external_id]]
        tracks = await self._dtos(norms)
        summary = await self._album_summary(album, len(tracks))
        return YandexAlbumDetail(album=summary, tracks=tracks)

    async def artist(self, artist_id: str) -> YandexArtistDetail:
        artist = MOCK_ARTISTS.get(str(artist_id))
        if artist is None:
            raise NotFoundError(
                "mock artist not found",
                code="yandex_artist_not_found",
                details={"artist_id": str(artist_id)[:64]},
            )
        norms = [MOCK_TRACKS[track_id] for track_id in MOCK_ARTIST_TRACKS[artist.external_id]]
        tracks = await self._dtos(norms)
        internal_id = (await self._repository.upsert_artists([artist]))[0]
        summary = ArtistSummary(
            id=internal_id,
            name=artist.name,
            cover_url=artist.cover_url,
            track_count=len(tracks),
            album_count=len({track.album.id for track in tracks if track.album}),
        )
        return YandexArtistDetail(artist=summary, tracks=tracks)

    async def search(
        self, query: str, *, type_: SearchType, limit: int, offset: int
    ) -> YandexSearchResults:
        if not query.strip():
            raise AppError(
                "search query must not be empty",
                code="invalid_query",
                status_code=400,
            )
        needle = query.strip().casefold()

        def matches(*values: str | None) -> bool:
            return any(needle in value.casefold() for value in values if value)

        def track_matches(track: NormalizedTrack) -> bool:
            return matches(
                track.title,
                *(artist.name for artist in track.artists),
                track.album.title if track.album is not None else None,
            )

        track_norms = [track for track in MOCK_TRACKS.values() if track_matches(track)]
        album_norms = [
            album
            for album in MOCK_ALBUMS.values()
            if matches(album.title, *(artist.name for artist in album.artists))
        ]
        artist_norms = [
            artist for artist in MOCK_ARTISTS.values() if matches(artist.name)
        ]
        playlist_norms = [
            _playlist_as_normalized(playlist)
            for playlist in MOCK_PLAYLISTS.values()
            if matches(playlist.title)
            or any(
                track_matches(MOCK_TRACKS[track_id])
                for track_id in playlist.track_ids
                if track_id in MOCK_TRACKS
            )
        ]

        if type_ != "all":
            track_norms = track_norms if type_ == "tracks" else []
            album_norms = album_norms if type_ == "albums" else []
            artist_norms = artist_norms if type_ == "artists" else []
            playlist_norms = playlist_norms if type_ == "playlists" else []

        track_items = await self._dtos(track_norms[offset : offset + limit])
        album_items = [
            await self._album_summary(album, album.track_count or 0)
            for album in album_norms[offset : offset + limit]
        ]
        artist_items = await self._artist_summaries(
            artist_norms[offset : offset + limit]
        )
        playlist_items = [
            self._playlist_dto(item) for item in playlist_norms[offset : offset + limit]
        ]
        return YandexSearchResults(
            tracks=LibraryPage(
                items=track_items,
                total=len(track_norms),
                limit=limit,
                offset=offset,
            ),
            albums=AlbumPage(
                items=album_items,
                total=len(album_norms),
                limit=limit,
                offset=offset,
            ),
            artists=ArtistPage(
                items=artist_items,
                total=len(artist_norms),
                limit=limit,
                offset=offset,
            ),
            playlists=YandexPlaylistPage(
                items=playlist_items, total=len(playlist_norms)
            ),
        )

    # ------------------------------------------------------------------- wave

    async def wave_start(self) -> WaveState:
        self._wave_ids = list(MOCK_WAVE_START)
        self._wave_index = 0
        self._batch_id = MOCK_BATCH_ID
        self._session_id = None
        state = await self._wave_state()
        await self._publish(state)
        return state

    async def wave_feedback(
        self,
        *,
        action: WaveFeedback,
        track_id: int,
        played_seconds: float = 0,
    ) -> WaveState:
        if action not in ("like", "dislike", "skip"):
            raise AppError(
                "unsupported wave feedback action",
                code="invalid_wave_action",
                status_code=400,
            )
        external = await self._repository.get_external_id(track_id)
        if external is None:
            raise NotFoundError(
                "track is not part of the wave",
                code="wave_track_unknown",
                details={"track_id": track_id},
            )
        yandex_id = int(external.split(":", 1)[0])
        if action == "like":
            if yandex_id in self._liked:
                self._liked.discard(yandex_id)
            else:
                self._liked.add(yandex_id)
        elif action == "dislike":
            self._liked.discard(yandex_id)
        else:
            self._wave_index = min(
                self._wave_index + 1, max(len(self._wave_ids) - 1, 0)
            )
            self._extend_wave()

        state = await self._wave_state()
        await self._publish(state)
        return state

    # -------------------------------------------------------------- streaming

    async def stream_info(self, external_id: str) -> YandexStreamInfo:
        yandex_id = str(external_id).split(":", 1)[0]
        norm = MOCK_TRACKS.get(yandex_id)
        return YandexStreamInfo(
            url=f"mock://stream/{yandex_id}",
            codec="mp3",
            bitrate_kbps=320,
            preview=bool(norm.preview) if norm is not None else False,
            expires_at=None,
        )

    async def stream_info_for_track(self, track_id: int) -> YandexStreamInfo:
        """Route helper: resolve by internal id (mock URLs need no token)."""

        external = await self._repository.get_external_id(track_id)
        if external is None:
            return YandexStreamInfo(
                url=f"mock://stream/{track_id}",
                codec="mp3",
                bitrate_kbps=320,
                preview=False,
                expires_at=None,
            )
        return await self.stream_info(external)

    # ---------------------------------------------------------------- helpers

    async def _dtos(self, norms: list[NormalizedTrack]) -> list[Track]:
        internal_ids = await self._repository.upsert_tracks(norms)
        tracks = await self._repository.list_tracks(internal_ids)
        result: list[Track] = []
        for track, norm in zip(tracks, norms, strict=True):
            update = {
                "source": "yandex",
                "external_id": norm.external_id,
                "preview": norm.preview,
                "liked": int(norm.yandex_id) in self._liked,
            }
            if norm.cover_url:
                update["cover_url"] = norm.cover_url
            result.append(track.model_copy(update=update))
        return result

    async def _album_summary(
        self, album: NormalizedAlbum, track_count: int
    ) -> AlbumSummary:
        artist_ref = None
        if album.artists:
            await self._repository.upsert_artists(album.artists)
            artist_id = await self._repository.get_artist_id(
                album.artists[0].external_id
            )
            if artist_id is not None:
                artist_ref = ArtistRef(id=artist_id, name=album.artists[0].name)
        internal_id = await self._repository.upsert_album(
            album, artist_id=artist_ref.id if artist_ref is not None else None
        )
        return AlbumSummary(
            id=internal_id,
            title=album.title,
            year=album.year,
            artist=artist_ref,
            cover_url=album.cover_url,
            track_count=track_count or (album.track_count or 0),
        )

    async def _artist_summaries(
        self, artists: list[NormalizedArtist]
    ) -> list[ArtistSummary]:
        if not artists:
            return []
        internal_ids = await self._repository.upsert_artists(artists)
        return [
            ArtistSummary(
                id=internal_id,
                name=artist.name,
                cover_url=artist.cover_url,
                track_count=len(MOCK_ARTIST_TRACKS.get(artist.external_id, ())),
                album_count=len(
                    {
                        MOCK_TRACKS[track_id].album.external_id
                        for track_id in MOCK_ARTIST_TRACKS.get(
                            artist.external_id, ()
                        )
                        if MOCK_TRACKS[track_id].album is not None
                    }
                ),
            )
            for artist, internal_id in zip(artists, internal_ids, strict=True)
        ]

    @staticmethod
    def _playlist_ref(playlist_id: str) -> tuple[str, int]:
        try:
            return parse_playlist_id(playlist_id)
        except ValueError as exc:
            raise AppError(
                'playlist_id must be "<uid>:<kind>"',
                code="invalid_playlist_id",
                status_code=400,
                details={"playlist_id": playlist_id[:64]},
            ) from exc

    @staticmethod
    def _playlist_dto(playlist: NormalizedPlaylist) -> YandexPlaylist:
        return YandexPlaylist(
            id=playlist.external_id,
            kind=playlist.kind,
            title=playlist.title,
            track_count=playlist.track_count,
            cover_url=playlist.cover_url,
            owner=playlist.owner,
        )

    async def _wave_state(self) -> WaveState:
        norms = [MOCK_TRACKS[track_id] for track_id in self._wave_ids]
        tracks = await self._dtos(norms)
        return WaveState(
            station=WAVE_STATION,
            batch_id=self._batch_id,
            session_id=self._session_id,
            tracks=tracks,
            current_index=min(self._wave_index, max(len(tracks) - 1, 0)),
            updated_at=utc_now_iso(),
        )

    def _extend_wave(self) -> None:
        guard = 0
        while len(self._wave_ids) - self._wave_index < MIN_WAVE_TRACKS:
            upcoming = self._wave_ids[self._wave_index :]
            remaining = [tid for tid in ALL_TRACK_IDS if tid not in upcoming]
            candidate = (
                remaining[0]
                if remaining
                else ALL_TRACK_IDS[
                    (len(self._wave_ids) + guard) % len(ALL_TRACK_IDS)
                ]
            )
            self._wave_ids.append(candidate)
            guard += 1
            if guard > len(ALL_TRACK_IDS) * 2:  # pragma: no cover - defensive
                break

    async def _publish(self, state: WaveState) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            "wave.updated", {"wave": state.model_dump(mode="json")}
        )

    async def describe(self) -> dict[str, object]:
        """Kept for the pre-P3 ``/system/status`` adapter description."""

        status = await self.auth_status()
        return {
            "backend": self.backend,
            "mode": self.mode,
            "available": True,
            "authenticated": status.authenticated,
            "note": "deterministic mock data; no network and no token",
        }

    async def close(self) -> None:
        return None
