"""Thin async wrapper over ``yandex_music.ClientAsync`` (version 3.2.2).

Only the calls frozen in docs/YANDEX_API.md are allowed here; adding a method
requires updating that document first (AGENTS.md §3). The wrapper owns no token:
it asks the auth manager for a client and maps library/network failures to the
application error envelope so routes never answer 500 for an upstream problem.
"""

from __future__ import annotations

import asyncio
import inspect
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from app.errors import (
    AppError,
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    UnavailableError,
)


@runtime_checkable
class YandexAuthProvider(Protocol):
    """Contract this wrapper needs from the auth manager (flow A).

    The manager may additionally expose ``refresh_access_token()``; when it does,
    ``YandexClient.client(refresh=True)`` calls it before asking for the client.
    """

    async def get_client(self) -> Any: ...


async def call_auth(manager: Any, *names: str, **kwargs: Any) -> Any:
    """Call the first auth-manager method name that exists.

    Flow A names the manager surface ``status``/``start_device_flow``/
    ``device_status``; the adapter protocol uses ``auth_status``/
    ``start_device_auth``/``device_auth_status``. Trying both keeps the two
    independently frozen interfaces compatible.
    """

    for name in names:
        handler = getattr(manager, name, None)
        if callable(handler):
            return await handler(**kwargs)
    raise ConfigurationError(
        "auth manager must expose one of: " + ", ".join(names)
    )


@lru_cache(maxsize=1)
def _yandex_exceptions() -> dict[str, type[BaseException]]:
    """Resolve library exceptions lazily (import is not needed in mock mode)."""

    try:
        from yandex_music import exceptions as ym_exceptions
    except ImportError as exc:  # pragma: no cover - pinned dependency
        raise ConfigurationError(
            "yandex-music 3.2.2 is required for real mode; run `uv sync`"
        ) from exc
    names = (
        "YandexMusicError",
        "UnauthorizedError",
        "NotFoundError",
        "BadRequestError",
        "NetworkError",
        "TimedOutError",
        "InvalidBitrateError",
        "SchemaMismatchError",
        "DeviceAuthError",
    )
    resolved: dict[str, type[BaseException]] = {}
    for name in names:
        found = getattr(ym_exceptions, name, None)
        if found is not None:
            resolved[name] = found
    return resolved


class YandexClient:
    """Every network call of the Yandex integration goes through this class."""

    def __init__(self, auth: YandexAuthProvider, *, timeout_s: float = 15.0) -> None:
        self._auth = auth
        self._timeout_s = timeout_s

    # ---------------------------------------------------------------- helpers

    async def client(self, *, refresh: bool = False) -> Any:
        """Return the shared ``ClientAsync`` instance from the auth manager."""

        getter = getattr(self._auth, "get_client", None)
        if getter is None:  # pragma: no cover - contract violation
            raise ConfigurationError(
                "auth manager does not expose get_client(); cannot talk to Yandex"
            )
        if refresh:
            if "refresh" in inspect.signature(getter).parameters:
                return await getter(refresh=True)
            hook = getattr(self._auth, "refresh_access_token", None)
            if callable(hook):
                await hook()
        return await getter()

    async def _guard(self, operation: str, awaitable: Any) -> Any:
        try:
            async with asyncio.timeout(self._timeout_s):
                return await awaitable
        except TimeoutError as exc:
            raise UnavailableError(
                "Yandex Music request timed out",
                code="yandex_timeout",
                details={"operation": operation},
            ) from exc
        except Exception as exc:  # mapped to AppError below
            raise self._map_error(operation, exc) from exc

    @staticmethod
    def _map_error(operation: str, exc: BaseException) -> AppError:
        """Translate a library exception into a stable API error (never a token)."""

        errors = _yandex_exceptions()

        def is_(name: str) -> bool:
            exc_type = errors.get(name)
            return exc_type is not None and isinstance(exc, exc_type)

        if is_("UnauthorizedError") or is_("DeviceAuthError"):
            return AuthenticationError(
                "Yandex Music authorization required",
                details={"operation": operation, "reason": "auth_required"},
            )
        if is_("TimedOutError") or is_("NetworkError"):
            return UnavailableError(
                "Yandex Music is unreachable",
                code="yandex_unavailable",
                details={"operation": operation},
            )
        if is_("NotFoundError"):
            return NotFoundError(
                "Yandex Music object not found",
                code="yandex_not_found",
                details={"operation": operation},
            )
        if is_("BadRequestError") or is_("InvalidBitrateError"):
            return AppError(
                "Yandex Music rejected the request",
                code="yandex_bad_request",
                status_code=400,
                details={"operation": operation},
            )
        if is_("SchemaMismatchError"):
            return UnavailableError(
                "Yandex Music response schema changed",
                code="yandex_schema_mismatch",
                details={"operation": operation},
            )
        return UnavailableError(
            "Yandex Music request failed",
            code="yandex_error",
            details={"operation": operation},
        )

    # ---------------------------------------------------------------- catalog

    async def likes(self) -> list[Any]:
        client = await self.client()
        result = await self._guard("users_likes_tracks", client.users_likes_tracks())
        return list(getattr(result, "tracks", None) or [])

    async def playlists(self) -> list[Any]:
        client = await self.client()
        result = await self._guard(
            "users_playlists_list", client.users_playlists_list()
        )
        if result is None:
            return []
        return list(result) if isinstance(result, list) else [result]

    async def playlist(self, uid: str, kind: int) -> Any | None:
        client = await self.client()
        result = await self._guard(
            "users_playlists", client.users_playlists(kind=[kind], user_id=uid)
        )
        if result is None:
            return None
        playlist = result[0] if isinstance(result, list) else result
        if playlist is not None and getattr(playlist, "client", None) is None:
            # Batch deserialization can leave ``client`` unset; fetch_tracks()
            # asserts a valid client before requesting the composition.
            playlist.client = client
        return playlist

    async def playlist_tracks(self, uid: str, kind: int) -> tuple[Any | None, list[Any]]:
        playlist = await self.playlist(uid, kind)
        if playlist is None:
            return None, []
        # ClientAsync objects must use the async shortcut; the sync
        # fetch_tracks() asserts a *sync* Client and would always fail here.
        fetch = getattr(playlist, "fetch_tracks_async", None)
        if fetch is None:
            fetch = getattr(playlist, "fetch_tracks", None)
        if fetch is None:  # pragma: no cover - contract violation
            raise UnavailableError(
                "Yandex Music playlist cannot be expanded",
                code="yandex_schema_mismatch",
                details={"operation": "Playlist.fetch_tracks"},
            )
        result = fetch()
        shorts = await self._guard("Playlist.fetch_tracks", result)
        return playlist, list(shorts or [])

    # ------------------------------------------------------- playlist editing

    async def playlist_create(
        self, title: str, *, visibility: str = "private"
    ) -> Any | None:
        client = await self.client()
        return await self._guard(
            "users_playlists_create",
            client.users_playlists_create(title, visibility=visibility),
        )

    async def playlist_rename(self, kind: int, name: str) -> Any | None:
        client = await self.client()
        return await self._guard(
            "users_playlists_name", client.users_playlists_name(kind, name)
        )

    async def playlist_delete(self, kind: int) -> bool:
        client = await self.client()
        result = await self._guard(
            "users_playlists_delete", client.users_playlists_delete(kind)
        )
        return bool(result)

    async def _playlist_revision(self, kind: int) -> int:
        client = await self.client()
        result = await self._guard(
            "users_playlists", client.users_playlists(kind=[kind])
        )
        playlist = result[0] if isinstance(result, list) and result else result
        revision = getattr(playlist, "revision", None) if playlist is not None else None
        return int(revision or 1)

    async def playlist_add_track(
        self,
        kind: int,
        track_id: str | int,
        album_id: str | int,
        *,
        at: int = 0,
    ) -> Any | None:
        client = await self.client()
        revision = await self._playlist_revision(kind)
        return await self._guard(
            "users_playlists_insert_track",
            client.users_playlists_insert_track(
                kind, track_id, album_id, at=max(at, 0), revision=revision
            ),
        )

    async def playlist_remove_track(self, kind: int, position: int) -> Any | None:
        client = await self.client()
        revision = await self._playlist_revision(kind)
        return await self._guard(
            "users_playlists_delete_track",
            client.users_playlists_delete_track(
                kind, max(position, 0), max(position, 0), revision=revision
            ),
        )

    async def tracks(self, track_ids: list[str | int]) -> list[Any]:
        if not track_ids:
            return []
        client = await self.client()
        result = await self._guard("tracks", client.tracks(list(track_ids)))
        return list(result or [])

    async def track(self, track_id: str | int) -> Any | None:
        items = await self.tracks([track_id])
        return items[0] if items else None

    async def album(self, album_id: str | int) -> Any | None:
        client = await self.client()
        return await self._guard(
            "albums_with_tracks", client.albums_with_tracks(album_id)
        )

    async def artist_tracks(
        self, artist_id: str | int, *, page: int = 0, page_size: int = 50
    ) -> Any | None:
        client = await self.client()
        return await self._guard(
            "artists_tracks",
            client.artists_tracks(artist_id, page=page, page_size=page_size),
        )

    async def search(self, text: str, *, type_: str = "all", page: int = 0) -> Any | None:
        client = await self.client()
        # The library expects singular values (track/album/artist/playlist),
        # while the public REST contract uses plural names.
        library_type = {
            "tracks": "track",
            "albums": "album",
            "artists": "artist",
            "playlists": "playlist",
        }.get(type_, type_)
        return await self._guard(
            "search", client.search(text, type_=library_type, page=page)
        )

    # -------------------------------------------------------------- streaming

    async def download_infos(
        self, track_id: str | int, *, refresh: bool = False
    ) -> list[Any]:
        client = await self.client(refresh=refresh)
        infos = await self._guard(
            "tracks_download_info",
            client.tracks_download_info(track_id, get_direct_links=True),
        )
        return list(infos or [])

    async def direct_link(self, download_info: Any, *, refresh: bool = False) -> str:
        await self.client(refresh=refresh)
        link = await self._guard(
            "DownloadInfo.get_direct_link_async",
            download_info.get_direct_link_async(),
        )
        if not link:
            raise UnavailableError(
                "Yandex Music did not return a stream link",
                code="stream_unavailable",
                details={"operation": "DownloadInfo.get_direct_link_async"},
            )
        return str(link)

    # ------------------------------------------------------------------- wave

    async def rotor_station_tracks(
        self, station: str, *, queue: str | int | None = None
    ) -> Any | None:
        client = await self.client()
        kwargs: dict[str, Any] = {}
        if queue is not None:
            kwargs["queue"] = queue
        return await self._guard(
            "rotor_station_tracks", client.rotor_station_tracks(station, **kwargs)
        )

    async def rotor_feedback(
        self,
        station: str,
        type_: str,
        *,
        track_id: str | int | None = None,
        total_played_seconds: float | None = None,
        batch_id: str | None = None,
    ) -> bool:
        client = await self.client()
        kwargs: dict[str, Any] = {}
        if track_id is not None:
            kwargs["track_id"] = track_id
        if total_played_seconds is not None:
            kwargs["total_played_seconds"] = total_played_seconds
        if batch_id is not None:
            kwargs["batch_id"] = batch_id
        return bool(
            await self._guard(
                "rotor_station_feedback",
                client.rotor_station_feedback(station, type_, **kwargs),
            )
        )

    # ---------------------------------------------------------- account likes

    async def likes_add(self, track_ids: list[str | int]) -> bool:
        return await self._account("users_likes_tracks_add", track_ids)

    async def likes_remove(self, track_ids: list[str | int]) -> bool:
        return await self._account("users_likes_tracks_remove", track_ids)

    async def dislikes_add(self, track_ids: list[str | int]) -> bool:
        return await self._account("users_dislikes_tracks_add", track_ids)

    async def dislikes_remove(self, track_ids: list[str | int]) -> bool:
        return await self._account("users_dislikes_tracks_remove", track_ids)

    async def _account(self, method: str, track_ids: list[str | int]) -> bool:
        if not track_ids:
            return False
        client = await self.client()
        handler = getattr(client, method)
        return bool(await self._guard(method, handler(list(track_ids))))

    async def close(self) -> None:
        """No-op: the ``ClientAsync`` session belongs to the auth manager."""

        return None
