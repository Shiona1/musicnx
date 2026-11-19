"""Frozen Yandex adapter contract shared by real and mock implementations.

Only methods confirmed in docs/YANDEX_API.md may be used by implementations.
API routes and UI are built against this interface; changing it requires the
integrator (see docs/API.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

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

WaveFeedback = Literal["like", "dislike", "skip"]
SearchType = Literal["all", "tracks", "albums", "artists", "playlists"]


@dataclass(frozen=True)
class YandexStreamInfo:
    """Temporary direct stream URL resolved immediately before playback."""

    url: str
    codec: str
    bitrate_kbps: int | None
    preview: bool
    expires_at: str | None = None


class YandexAdapter(Protocol):
    """High-level adapter: normalizes Yandex data into internal DTOs.

    Implementations own no HTTP routes; they may use the token store/DB
    repository. Tokens must never leave the adapter.
    """

    mode: str

    async def auth_status(self) -> YandexAuthStatus: ...

    async def start_device_auth(self) -> YandexDeviceStart: ...

    async def device_auth_status(self, flow_id: str) -> YandexDeviceStatus: ...

    async def set_manual_token(self, token: str) -> YandexAuthStatus: ...

    async def logout(self) -> None: ...

    async def likes(self, *, limit: int, offset: int) -> LibraryPage: ...

    async def playlists(self) -> YandexPlaylistPage: ...

    async def playlist(
        self, playlist_id: str, *, limit: int, offset: int
    ) -> tuple[YandexPlaylist, LibraryPage]: ...

    async def album(self, album_id: str) -> YandexAlbumDetail: ...

    async def artist(self, artist_id: str) -> YandexArtistDetail: ...

    async def search(
        self, query: str, *, type_: SearchType, limit: int, offset: int
    ) -> YandexSearchResults: ...

    async def wave_start(self) -> WaveState: ...

    async def wave_feedback(
        self, *, action: WaveFeedback, track_id: int, played_seconds: float = 0
    ) -> WaveState: ...

    async def stream_info(self, external_id: str) -> YandexStreamInfo: ...

    async def close(self) -> None: ...
