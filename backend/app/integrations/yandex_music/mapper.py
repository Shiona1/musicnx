"""Pure mapping from ``yandex_music`` 3.2.2 models to normalized structures.

Only fields listed in docs/YANDEX_API.md §2 are read. The mapper performs no IO,
holds no state and does not import the library: models are duck-typed, so schema
drift degrades to a logged warning and a skipped item instead of a crash
(AGENTS.md §3). Mock fixtures use the same normalized structs, which keeps the
mock and real data shapes identical (YANDEX_API §6).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.timeutils import to_iso
from app.logging_setup import get_logger

logger = get_logger("yandex.mapper")

COVER_HOST = "avatars.yandex.net"
COVER_SIZES = (64, 300, 1000)
DEFAULT_COVER_SIZE = 300
# Yandex cover URIs carry a "%%" size placeholder; some already embed "300x300".
_SIZE_PLACEHOLDER = "%%"
_SIZE_TOKEN = re.compile(r"\d{2,4}x\d{2,4}")

_schema_warnings: Counter[str] = Counter()


def _warn_once(key: str, message: str) -> None:
    """Log the first occurrence of a schema drift and keep counting silently."""

    _schema_warnings[key] += 1
    if _schema_warnings[key] == 1:
        logger.warning("schema mismatch (%s): %s; degrading gracefully", key, message)


def schema_warning_counts() -> dict[str, int]:
    """Expose drift counters for diagnostics and tests."""

    return dict(_schema_warnings)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read one attribute, never raising on broken models or properties."""

    try:
        return getattr(obj, name, default)
    except Exception as exc:  # any model bug must not break mapping
        _warn_once(f"{type(obj).__name__}.{name}", f"attribute read failed: {exc!r}")
        return default


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        _warn_once(f"int:{type(value).__name__}", f"expected an integer, got {value!r}")
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value).strip() or None


def _as_list(value: Any, key: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return list(value)
    _warn_once(key, f"expected a list, got {type(value).__name__}")
    return []


# --------------------------------------------------------------------- covers


def cover_path(cover_uri: str | None, size: int = DEFAULT_COVER_SIZE) -> str | None:
    """Validate a Yandex cover URI and return its resized path, or ``None``.

    Only ``avatars.yandex.net`` is accepted so a compromised response cannot
    turn the cover proxy into an SSRF gadget.
    """

    uri = _as_str(cover_uri)
    if uri is None:
        return None
    if size not in COVER_SIZES:
        size = DEFAULT_COVER_SIZE

    split = urlsplit(uri if "://" in uri else f"https://{uri}")
    if split.scheme not in {"http", "https"}:
        return None
    if (split.hostname or "").lower() != COVER_HOST:
        _warn_once("cover.host", f"refused cover host {split.hostname!r}")
        return None
    path = split.path
    if not path or ".." in path or "\\" in path or "\x00" in path:
        _warn_once("cover.path", "refused an unsafe cover path")
        return None
    return _resize_cover_path(path, size)


def cover_url(cover_uri: str | None, size: int = DEFAULT_COVER_SIZE) -> str | None:
    """Turn a Yandex cover URI into an https URL at the requested size."""

    path = cover_path(cover_uri, size)
    return f"https://{COVER_HOST}{path}" if path is not None else None


def _resize_cover_path(path: str, size: int) -> str:
    dimensions = f"{size}x{size}"
    if _SIZE_PLACEHOLDER in path:
        return path.replace(_SIZE_PLACEHOLDER, dimensions)
    # Already sized (".../300x300"): replace the last size token, if any.
    matches = list(_SIZE_TOKEN.finditer(path))
    if matches:
        last = matches[-1]
        return f"{path[: last.start()]}{dimensions}{path[last.end() :]}"
    return path


def _cover_object_url(cover: Any, size: int = DEFAULT_COVER_SIZE) -> str | None:
    """Album/Artist/Playlist carry a ``cover`` object with a ``uri`` field."""

    if cover is None:
        return None
    uri = cover if isinstance(cover, str) else _field(cover, "uri")
    return cover_url(uri, size)


# -------------------------------------------------------------------- structs


@dataclass(frozen=True)
class NormalizedArtist:
    """A Yandex artist reduced to what the SQLite catalog stores."""

    external_id: str
    name: str
    cover_url: str | None = None


@dataclass(frozen=True)
class NormalizedAlbum:
    external_id: str
    title: str
    year: int | None = None
    cover_url: str | None = None
    track_count: int | None = None
    artists: tuple[NormalizedArtist, ...] = ()


@dataclass(frozen=True)
class NormalizedTrack:
    """A Yandex track in the normalized form used by repository and DTOs."""

    external_id: str
    yandex_id: str
    album_id: str | None
    title: str
    artists: tuple[NormalizedArtist, ...] = ()
    album: NormalizedAlbum | None = None
    duration_ms: int | None = None
    year: int | None = None
    available: bool = True
    preview: bool = False
    cover_url: str | None = None
    explicit: bool = False
    lyrics_available: bool = False
    filename: str | None = None


@dataclass(frozen=True)
class NormalizedPlaylist:
    external_id: str
    uid: str
    kind: int
    title: str
    track_count: int = 0
    cover_url: str | None = None
    owner: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class NormalizedDownloadInfo:
    codec: str
    bitrate_kbps: int | None = None
    preview: bool = False
    direct: str | None = None


@dataclass(frozen=True)
class NormalizedSearchResults:
    tracks: tuple[NormalizedTrack, ...] = ()
    albums: tuple[NormalizedAlbum, ...] = ()
    artists: tuple[NormalizedArtist, ...] = ()
    playlists: tuple[NormalizedPlaylist, ...] = ()
    track_total: int = 0
    album_total: int = 0
    artist_total: int = 0
    playlist_total: int = 0


@dataclass(frozen=True)
class NormalizedWave:
    station: str
    batch_id: str | None = None
    session_id: str | None = None
    tracks: tuple[NormalizedTrack, ...] = ()
    liked: tuple[bool, ...] = field(default=())

    @property
    def track_ids(self) -> tuple[str, ...]:
        return tuple(track.yandex_id for track in self.tracks)


# ---------------------------------------------------------------- converters


def map_artist(raw: Any, size: int = DEFAULT_COVER_SIZE) -> NormalizedArtist | None:
    artist_id = _as_str(_field(raw, "id"))
    if artist_id is None:
        _warn_once("Artist.id", "artist without id skipped")
        return None
    name = _as_str(_field(raw, "name")) or "Unknown Artist"
    return NormalizedArtist(
        external_id=artist_id,
        name=name,
        cover_url=_cover_object_url(_field(raw, "cover"), size),
    )


def map_artists(items: Sequence[Any] | None, size: int = DEFAULT_COVER_SIZE) -> tuple[
    NormalizedArtist, ...
]:
    result = [mapped for item in items or [] if (mapped := map_artist(item, size)) is not None]
    return tuple(result)


def map_album(raw: Any, size: int = DEFAULT_COVER_SIZE) -> NormalizedAlbum | None:
    album_id = _as_str(_field(raw, "id"))
    if album_id is None:
        _warn_once("Album.id", "album without id skipped")
        return None
    return NormalizedAlbum(
        external_id=album_id,
        title=_as_str(_field(raw, "title")) or "",
        year=_as_int(_field(raw, "year")),
        cover_url=cover_url(_field(raw, "cover_uri"), size)
        or _cover_object_url(_field(raw, "cover"), size),
        track_count=_as_int(_field(raw, "track_count")),
        artists=map_artists(_field(raw, "artists"), size),
    )


def map_track(raw: Any, size: int = DEFAULT_COVER_SIZE) -> NormalizedTrack | None:
    """Map one Track; returns ``None`` when the identity itself is missing."""

    track_id = _as_str(_field(raw, "id"))
    if track_id is None:
        _warn_once("Track.id", "track without id skipped")
        return None

    artists = map_artists(_field(raw, "artists"), size)
    albums = _as_list(_field(raw, "albums"), "Track.albums")
    album = next(
        (mapped for item in albums if (mapped := map_album(item, size)) is not None),
        None,
    )
    album_id = album.external_id if album is not None else None

    available_raw = _field(raw, "available")
    available = True if available_raw is None else bool(available_raw)
    if _field(raw, "error"):
        available = False

    return NormalizedTrack(
        external_id=f"{track_id}:{album_id or track_id}",
        yandex_id=track_id,
        album_id=album_id,
        title=_as_str(_field(raw, "title")) or f"Track {track_id}",
        artists=artists,
        album=album,
        duration_ms=_as_int(_field(raw, "duration_ms")),
        year=album.year if album is not None else None,
        available=available,
        # ``preview_duration_ms`` is populated for most full tracks too; it is
        # not an access restriction. The authoritative preview flag comes from
        # DownloadInfo.preview when a stream is resolved.
        preview=False,
        cover_url=cover_url(_field(raw, "cover_uri"), size)
        or (album.cover_url if album is not None else None),
        explicit=bool(_field(raw, "explicit", False)),
        lyrics_available=bool(_field(raw, "lyrics_available", False)),
        filename=_as_str(_field(raw, "filename")),
    )


def map_tracks(items: Sequence[Any] | None, size: int = DEFAULT_COVER_SIZE) -> list[
    NormalizedTrack
]:
    result: list[NormalizedTrack] = []
    for item in items or []:
        mapped = map_track(item, size)
        if mapped is not None:
            result.append(mapped)
    return result


def album_tracks(raw: Any, size: int = DEFAULT_COVER_SIZE) -> list[NormalizedTrack]:
    """Flatten ``Album.volumes`` (a list of per-disc track lists)."""

    tracks: list[NormalizedTrack] = []
    for volume in _as_list(_field(raw, "volumes"), "Album.volumes"):
        tracks.extend(map_tracks(_as_list(volume, "Album.volume"), size))
    return tracks


def parse_playlist_id(playlist_id: str) -> tuple[str, int]:
    """Split the API form ``"<uid>:<kind>"`` into owner and playlist kind."""

    uid, separator, kind = playlist_id.partition(":")
    if not separator or not uid or not kind.isdigit():
        raise ValueError(f"invalid Yandex playlist id: {playlist_id[:64]!r}")
    return uid, int(kind)


def map_playlist(raw: Any, size: int = DEFAULT_COVER_SIZE) -> NormalizedPlaylist | None:
    uid = _as_str(_field(raw, "uid"))
    kind = _as_int(_field(raw, "kind"))
    if uid is None or kind is None:
        _warn_once("Playlist.identity", "playlist without uid/kind skipped")
        return None
    cover = _cover_object_url(_field(raw, "cover"), size) or cover_url(
        _field(raw, "og_image"), size
    )
    return NormalizedPlaylist(
        external_id=f"{uid}:{kind}",
        uid=uid,
        kind=kind,
        title=_as_str(_field(raw, "title")) or f"Playlist {kind}",
        track_count=_as_int(_field(raw, "track_count")) or 0,
        cover_url=cover,
        owner=uid,
        revision=_as_int(_field(raw, "revision")),
    )


def track_short_ids(items: Sequence[Any] | None) -> list[str]:
    """Extract track ids from ``TracksList.tracks`` / ``Playlist.fetch_tracks``.

    ``TrackShort.id`` is the id; ``TrackShort.track`` may be ``None`` (the full
    track is fetched separately with ``tracks([ids])``). Order is preserved and
    duplicates are dropped (a playlist may not, but a broken response can).
    """

    ids: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        track_id = _as_str(_field(item, "id"))
        if track_id is None:
            embedded = _field(item, "track")
            track_id = _as_str(_field(embedded, "id")) if embedded is not None else None
        if track_id is None:
            _warn_once("TrackShort.id", "track short without id skipped")
            continue
        if track_id in seen:
            continue
        seen.add(track_id)
        ids.append(track_id)
    return ids


def _search_section(raw: Any) -> tuple[list[Any], int | None]:
    section = _field(raw, "results")
    items = _as_list(section, "Search.section")
    total = _as_int(_field(raw, "total"))
    return items, total


def map_search(raw: Any, size: int = DEFAULT_COVER_SIZE) -> NormalizedSearchResults:
    """Map ``Search`` into four normalized sections with their server totals."""

    tracks_raw, tracks_total = _search_section(_field(raw, "tracks"))
    albums_raw, albums_total = _search_section(_field(raw, "albums"))
    artists_raw, artists_total = _search_section(_field(raw, "artists"))
    playlists_raw, playlists_total = _search_section(_field(raw, "playlists"))

    tracks = tuple(map_tracks(tracks_raw, size))
    albums = tuple(
        mapped for item in albums_raw if (mapped := map_album(item, size)) is not None
    )
    artists = tuple(
        mapped for item in artists_raw if (mapped := map_artist(item, size)) is not None
    )
    playlists = tuple(
        mapped
        for item in playlists_raw
        if (mapped := map_playlist(item, size)) is not None
    )
    return NormalizedSearchResults(
        tracks=tracks,
        albums=albums,
        artists=artists,
        playlists=playlists,
        track_total=tracks_total if tracks_total is not None else len(tracks),
        album_total=albums_total if albums_total is not None else len(albums),
        artist_total=artists_total if artists_total is not None else len(artists),
        playlist_total=(
            playlists_total if playlists_total is not None else len(playlists)
        ),
    )


def map_download_infos(items: Sequence[Any] | None) -> list[NormalizedDownloadInfo]:
    result: list[NormalizedDownloadInfo] = []
    for item in items or []:
        codec = _as_str(_field(item, "codec"))
        if codec is None:
            _warn_once("DownloadInfo.codec", "download info without codec skipped")
            continue
        result.append(
            NormalizedDownloadInfo(
                codec=codec.lower(),
                bitrate_kbps=_as_int(_field(item, "bitrate_in_kbps")),
                preview=bool(_field(item, "preview", False)),
                # ``DownloadInfo.direct`` is a bool flag in yandex_music 3.2.2;
                # the signed URL is ``direct_link`` (gate doc §3).
                direct=_as_str(_field(item, "direct_link")),
            )
        )
    return result


# Preference order per configured quality; FLAC is never transcoded, only picked.
QUALITY_PREFERENCE: dict[str, tuple[str, ...]] = {
    "flac": ("flac", "aac", "mp3"),
    "aac": ("aac", "mp3", "flac"),
    "mp3": ("mp3", "aac", "flac"),
}


def select_download_info(
    infos: Sequence[NormalizedDownloadInfo], quality: str
) -> NormalizedDownloadInfo | None:
    """Pick the best non-preview stream, highest bitrate within a codec.

    Preview entries are used only when nothing else exists, and the caller keeps
    the honest ``preview=True`` badge in that case.
    """

    if not infos:
        return None
    full = [info for info in infos if not info.preview]
    pool = full or list(infos)
    order = QUALITY_PREFERENCE.get(quality, QUALITY_PREFERENCE["mp3"])
    for codec in order:
        candidates = [info for info in pool if info.codec == codec]
        if candidates:
            return max(candidates, key=lambda info: info.bitrate_kbps or 0)
    return max(pool, key=lambda info: info.bitrate_kbps or 0)


def map_wave(raw: Any, station: str, size: int = DEFAULT_COVER_SIZE) -> NormalizedWave:
    """Map ``StationTracksResult`` preserving sequence order and liked flags."""

    tracks: list[NormalizedTrack] = []
    liked: list[bool] = []
    for item in _as_list(_field(raw, "sequence"), "StationTracksResult.sequence"):
        mapped = map_track(_field(item, "track"), size)
        if mapped is None:
            continue
        tracks.append(mapped)
        liked.append(bool(_field(item, "liked", False)))
    return NormalizedWave(
        station=station,
        batch_id=_as_str(_field(raw, "batch_id")),
        session_id=_as_str(_field(raw, "radio_session_id")),
        tracks=tuple(tracks),
        liked=tuple(liked),
    )


def direct_link_expiry(url: str) -> str | None:
    """Extract a signed-link expiry as ISO-8601, if the URL carries one."""

    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for key in ("expires_at", "expires", "expire"):
        raw = (query.get(key) or [None])[0]
        if raw is None:
            continue
        try:
            timestamp = int(raw)
        except (TypeError, ValueError):
            continue
        try:
            return to_iso(datetime.fromtimestamp(timestamp, UTC))
        except (OverflowError, OSError, ValueError):
            return None
    return None
