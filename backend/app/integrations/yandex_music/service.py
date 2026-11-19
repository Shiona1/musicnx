"""Yandex adapter factory: mock by default, real behind ``mode=real``.

Construction never touches the network or the token store: the real adapter only
builds wrappers around the auth manager. Routes consume the ``YandexAdapter``
protocol, so switching modes changes no business logic (AGENTS.md §4).
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import (
    LibraryPage,
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
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.errors import ConfigurationError, NotFoundError, UnavailableError
from app.integrations.yandex_music.base import (
    SearchType,
    WaveFeedback,
    YandexAdapter,
    YandexStreamInfo,
)
from app.integrations.yandex_music.catalog import YandexCatalogService
from app.integrations.yandex_music.client import (
    YandexAuthProvider,
    YandexClient,
    call_auth,
)
from app.integrations.yandex_music.mapper import (
    NormalizedPlaylist,
    map_playlist,
    parse_playlist_id,
)
from app.integrations.yandex_music.mock_adapter import MockYandexAdapter
from app.integrations.yandex_music.repository import YandexRepository
from app.integrations.yandex_music.streaming import YandexStreamingService
from app.integrations.yandex_music.wave import WaveService


class RealYandexAdapter:
    """``YandexAdapter`` over the auth manager, catalog, streaming and wave."""

    mode = "real"

    def __init__(
        self,
        *,
        settings: Settings,
        repository: YandexRepository,
        auth: YandexAuthProvider,
        bus: EventBus | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._auth = auth
        self._bus = bus
        self._client = YandexClient(
            auth, timeout_s=settings.yandex.request_timeout_s
        )
        self._catalog = YandexCatalogService(self._client, repository)
        self._streaming = YandexStreamingService(self._client, repository, settings)
        self._wave = WaveService(self._client, repository, bus)

    @property
    def streaming(self) -> YandexStreamingService:
        """Expose the streaming service so ``/stream`` can resolve by track id."""

        return self._streaming

    # ------------------------------------------------------------------- auth

    async def _auth_call(self, *names: str, **kwargs: Any) -> Any:
        return await call_auth(self._auth, *names, **kwargs)

    async def auth_status(self) -> YandexAuthStatus:
        return await self._auth_call("auth_status", "status")

    async def start_device_auth(self) -> YandexDeviceStart:
        return await self._auth_call("start_device_auth", "start_device_flow")

    async def device_auth_status(self, flow_id: str) -> YandexDeviceStatus:
        return await self._auth_call(
            "device_auth_status", "device_status", flow_id=flow_id
        )

    async def set_manual_token(self, token: str) -> YandexAuthStatus:
        return await self._auth_call("set_manual_token", token=token)

    async def logout(self) -> None:
        await self._auth_call("logout")

    # ---------------------------------------------------------------- catalog

    async def likes(self, *, limit: int, offset: int) -> LibraryPage:
        page = await self._catalog.likes(limit=limit, offset=offset)
        await self._catalog_event("likes", len(page.items))
        return page

    async def playlists(self) -> YandexPlaylistPage:
        page = await self._catalog.playlists()
        await self._catalog_event("playlists", len(page.items))
        return page

    async def playlist(
        self, playlist_id: str, *, limit: int, offset: int
    ) -> tuple[YandexPlaylist, LibraryPage]:
        playlist, page = await self._catalog.playlist(
            playlist_id, limit=limit, offset=offset
        )
        await self._catalog_event("playlist", len(page.items))
        return playlist, page

    async def create_playlist(
        self, title: str, *, visibility: str = "private"
    ) -> YandexPlaylist:
        raw = await self._client.playlist_create(title, visibility=visibility)
        normalized = map_playlist(raw) if raw is not None else None
        if normalized is None:
            raise UnavailableError(
                "Yandex Music returned no playlist after create",
                code="yandex_schema_mismatch",
            )
        await self._repository.upsert_playlist(normalized)
        await self._catalog_event("playlists", 1)
        return self._playlist_dto(normalized)

    async def rename_playlist(self, playlist_id: str, title: str) -> YandexPlaylist:
        uid, kind = parse_playlist_id(playlist_id)
        raw = await self._client.playlist_rename(kind, title)
        normalized = map_playlist(raw) if raw is not None else None
        if normalized is None:
            # Some responses omit the full object; re-read it.
            playlist, _ = await self._catalog.playlist(
                f"{uid}:{kind}", limit=1, offset=0
            )
            return playlist
        await self._repository.upsert_playlist(normalized)
        await self._catalog_event("playlists", 1)
        return self._playlist_dto(normalized)

    async def delete_playlist(self, playlist_id: str) -> None:
        _, kind = parse_playlist_id(playlist_id)
        if not await self._client.playlist_delete(kind):
            raise UnavailableError(
                "Yandex Music did not delete the playlist",
                code="yandex_playlist_delete_failed",
            )
        await self._catalog_event("playlists", 0)

    async def add_playlist_track(
        self, playlist_id: str, track_id: int, *, at: int = 0
    ) -> YandexPlaylist:
        uid, kind = parse_playlist_id(playlist_id)
        external = await self._repository.get_external_id(track_id)
        if external is None:
            raise NotFoundError(
                "track has no Yandex source",
                details={"track_id": track_id},
            )
        yandex_track, _, album = external.partition(":")
        raw = await self._client.playlist_add_track(
            kind, yandex_track, album or yandex_track, at=at
        )
        normalized = map_playlist(raw) if raw is not None else None
        if normalized is not None:
            await self._repository.upsert_playlist(normalized)
            return self._playlist_dto(normalized)
        playlist, _ = await self._catalog.playlist(
            f"{uid}:{kind}", limit=1, offset=0
        )
        return playlist

    async def remove_playlist_track(
        self, playlist_id: str, position: int
    ) -> YandexPlaylist:
        uid, kind = parse_playlist_id(playlist_id)
        await self._client.playlist_remove_track(kind, position)
        playlist, _ = await self._catalog.playlist(
            f"{uid}:{kind}", limit=1, offset=0
        )
        return playlist

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

    async def album(self, album_id: str) -> YandexAlbumDetail:
        detail = await self._catalog.album(album_id)
        await self._catalog_event("album", len(detail.tracks))
        return detail

    async def artist(self, artist_id: str) -> YandexArtistDetail:
        detail = await self._catalog.artist(artist_id)
        await self._catalog_event("artist", len(detail.tracks))
        return detail

    async def artist_tracks(
        self, artist_id: str, *, limit: int, offset: int
    ) -> LibraryPage:
        page = await self._catalog.artist_tracks_page(
            artist_id, limit=limit, offset=offset
        )
        await self._catalog_event("artist", len(page.items))
        return page

    async def search(
        self, query: str, *, type_: SearchType, limit: int, offset: int
    ) -> YandexSearchResults:
        result = await self._catalog.search(
            query, type_=type_, limit=limit, offset=offset
        )
        await self._catalog_event("search", len(result.tracks.items))
        return result

    async def _catalog_event(self, kind: str, count: int) -> None:
        if self._bus is not None:
            await self._bus.publish(
                "yandex.catalog_updated", {"kind": kind, "count": count}
            )

    # ------------------------------------------------------------------- wave

    async def wave_start(self) -> WaveState:
        return await self._wave.start()

    async def wave_feedback(
        self, *, action: WaveFeedback, track_id: int, played_seconds: float = 0
    ) -> WaveState:
        return await self._wave.feedback(
            action=action, track_id=track_id, played_seconds=played_seconds
        )

    # -------------------------------------------------------------- streaming

    async def stream_info(self, external_id: str) -> YandexStreamInfo:
        return await self._streaming.stream_info(external_id)

    async def stream_info_for_track(self, track_id: int) -> YandexStreamInfo:
        """Route helper: resolve by internal ``track_id`` (``/stream/{id}``)."""

        return await self._streaming.stream_info(track_id)

    async def close(self) -> None:
        """No-op: the ``ClientAsync`` lifecycle belongs to the auth manager."""

        return None

    async def describe(self) -> dict[str, Any]:
        """Safe adapter status for ``/system/status`` (never tokens)."""

        try:
            status = await self.auth_status()
        except Exception:  # pragma: no cover - status must never break health
            status = YandexAuthStatus(authenticated=False)
        return {
            "mode": self.mode,
            "enabled": bool(self._settings.yandex.enabled),
            "authenticated": status.authenticated,
        }


def create_yandex_adapter(
    settings: Settings,
    bus: EventBus,
    db: Database,
    tracks: TrackRepository,
    auth_manager: YandexAuthProvider | None = None,
) -> YandexAdapter:
    """Build the adapter for the configured mode.

    Mock mode needs no token and no network; real mode requires
    ``yandex.enabled`` and an auth manager (flow A).
    """

    repository = YandexRepository(db, tracks)
    if settings.mode == "mock":
        # Flow A owns the mock device flow; the adapter delegates when present.
        return MockYandexAdapter(repository, bus=bus, auth=auth_manager)
    if auth_manager is None:
        raise ConfigurationError(
            "mode=real requires a YandexAuthManager instance for the auth manager"
        )
    return RealYandexAdapter(
        settings=settings, repository=repository, auth=auth_manager, bus=bus
    )
