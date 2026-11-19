"""Contract tests for the P3 Yandex fixtures.

The JSON files under ``tests/fixtures/yandex`` are the mock adapter's canned
answers. They must mirror the normalized DTOs from docs/API.md §6 and
docs/YANDEX_API.md §6 -- never raw ``yandex_music`` objects and never token
material. The same catalog ids must stay coherent across every page.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from app.core.models import (
    AlbumPage,
    ArtistPage,
    LibraryPage,
    Track,
    WaveState,
    YandexAlbumDetail,
    YandexArtistDetail,
    YandexAuthStatus,
    YandexDeviceStart,
    YandexPlaylist,
    YandexPlaylistPage,
    YandexSearchResults,
)
from pydantic import BaseModel

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "yandex"

#: Fixtures validated by a core DTO. ``playlist.json`` is a pair
#: ``{playlist, tracks}`` and is validated separately below.
MODEL_FIXTURES: dict[str, type[BaseModel]] = {
    "auth_status.json": YandexAuthStatus,
    "device_start.json": YandexDeviceStart,
    "likes.json": LibraryPage,
    "playlists.json": YandexPlaylistPage,
    "album.json": YandexAlbumDetail,
    "artist.json": YandexArtistDetail,
    "search.json": YandexSearchResults,
    "wave.json": WaveState,
}

EXPECTED_FIXTURES = frozenset(
    {
        "album.json",
        "artist.json",
        "auth_status.json",
        "device_start.json",
        "error_auth.json",
        "error_region.json",
        "likes.json",
        "playlist.json",
        "playlists.json",
        "search.json",
        "stream.json",
        "wave.json",
    }
)

SECRET_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_")
FORBIDDEN_KEYS = {"token", "access_token", "refresh_token", "device_code", "authorization"}
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
EXTERNAL_ID = re.compile(r"^\d+:\d+$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]*$")

QR_PNG_PREFIX = "data:image/png;base64,"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict), f"{name} must be a JSON object"
    return payload


def fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


def _iter_keys(value: Any) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_keys(item)


def _parse_timestamp(value: str) -> datetime:
    assert RFC3339.match(value), f"{value!r} is not RFC3339 with milliseconds"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assert_cover_url(value: str | None) -> None:
    assert value, "online Yandex tracks must carry a cover_url"
    parsed = urlparse(value)
    if parsed.scheme:
        assert parsed.scheme == "https", value
        assert parsed.netloc, value
    else:
        # docs/API.md §6: cover_url may point at our own artwork cache.
        assert value.startswith("/api/v1/artwork/"), value
    assert "token" not in value.lower(), value


def _assert_catalog_track(track: Track, canonical: dict[int, dict[str, Any]]) -> None:
    assert track.id in canonical, f"unknown track id {track.id}"
    assert track.model_dump(mode="json") == canonical[track.id], (
        f"track {track.id} differs from the canonical likes.json entry"
    )


def _assert_pages_complete(
    page: LibraryPage | AlbumPage | ArtistPage | YandexPlaylistPage,
) -> None:
    assert page.offset >= 0
    assert page.limit > 0
    assert len(page.items) <= page.limit
    assert page.total == page.offset + len(page.items), (
        "fixture pages are complete, so total must equal offset + len(items)"
    )


def _canonical_tracks() -> dict[int, dict[str, Any]]:
    page = LibraryPage.model_validate(load_fixture("likes.json"))
    canonical = {track.id: track.model_dump(mode="json") for track in page.items}
    assert canonical, "likes.json must seed the canonical catalog"
    assert len({entry["external_id"] for entry in canonical.values()}) == len(canonical)
    return canonical


def _canonical_artist_ids() -> set[int]:
    ids: set[int] = set()
    for entry in _canonical_tracks().values():
        ids.update(artist["id"] for artist in entry["artists"])
    return ids


def _canonical_album_ids() -> set[int]:
    return {
        entry["album"]["id"]
        for entry in _canonical_tracks().values()
        if entry["album"] is not None
    }


def _validated(name: str, model: type[BaseModel]) -> Any:
    return model.model_validate(load_fixture(name))


# --------------------------------------------------------------------------- #
# Shape and secrecy
# --------------------------------------------------------------------------- #


def test_fixture_set_is_exactly_the_p3_contract() -> None:
    assert {path.name for path in fixture_paths()} == EXPECTED_FIXTURES


def test_no_secret_markers_in_any_fixture() -> None:
    assert fixture_paths(), "no fixtures found"
    for path in fixture_paths():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in SECRET_MARKERS:
            assert marker not in lowered, f"{path.name} leaks {marker!r}"
        for key in _iter_keys(json.loads(text)):
            assert key.lower() not in FORBIDDEN_KEYS, (
                f"{path.name} contains forbidden field {key!r}"
            )


@pytest.mark.parametrize(("name", "model"), MODEL_FIXTURES.items(), ids=MODEL_FIXTURES.keys())
def test_fixture_matches_core_model(name: str, model: type[BaseModel]) -> None:
    payload = load_fixture(name)
    validated = model.model_validate(payload)
    # The fixture must survive the same JSON round-trip the API performs.
    assert json.loads(validated.model_dump_json()) == payload


def test_playlist_detail_matches_its_models() -> None:
    payload = load_fixture("playlist.json")
    YandexPlaylist.model_validate(payload["playlist"])
    LibraryPage.model_validate(payload["tracks"])


# --------------------------------------------------------------------------- #
# Auth / device flow
# --------------------------------------------------------------------------- #


def test_auth_status_fixture_is_authenticated() -> None:
    status = _validated("auth_status.json", YandexAuthStatus)
    assert status.authenticated is True
    assert status.login
    assert status.source in {"device", "manual"}
    assert status.expires_at is not None
    _parse_timestamp(status.expires_at)


def test_device_start_qr_is_a_real_png_data_uri() -> None:
    start = _validated("device_start.json", YandexDeviceStart)
    assert re.fullmatch(r"[0-9a-f]{32}", start.flow_id), start.flow_id
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", start.user_code), start.user_code
    assert start.verification_url.startswith("https://")
    assert start.expires_in > 0
    assert start.interval > 0
    assert start.qr_data_uri is not None
    assert start.qr_data_uri.startswith(QR_PNG_PREFIX)
    png = base64.b64decode(start.qr_data_uri.removeprefix(QR_PNG_PREFIX))
    assert png.startswith(PNG_MAGIC), "qr_data_uri must decode to a PNG"


# --------------------------------------------------------------------------- #
# Catalog coherence
# --------------------------------------------------------------------------- #


def test_likes_page_is_online_yandex_catalog() -> None:
    page = _validated("likes.json", LibraryPage)
    _assert_pages_complete(page)
    assert page.offset == 0
    assert page.total == len(page.items) == 4

    previews = {track.preview for track in page.items}
    assert previews == {True, False}, "catalog must cover full tracks and 30s previews"

    for track in page.items:
        assert track.source == "yandex"
        assert track.availability == "online"
        assert track.has_online_source is True
        assert track.has_local_file is False
        assert track.file_missing is False
        assert track.source_unavailable is False
        assert track.external_id is not None
        assert EXTERNAL_ID.match(track.external_id), track.external_id
        assert track.external_id.split(":")[0].isdigit()
        assert track.duration_ms is not None and track.duration_ms > 0
        assert track.liked is True
        assert track.artists, "TrackDTO.artists must not be empty"
        assert track.album is not None
        _assert_cover_url(track.cover_url)


def test_playlists_page_agrees_with_playlist_detail() -> None:
    page = _validated("playlists.json", YandexPlaylistPage)
    detail_payload = load_fixture("playlist.json")
    detail_summary = YandexPlaylist.model_validate(detail_payload["playlist"])
    detail_tracks = LibraryPage.model_validate(detail_payload["tracks"])

    assert page.total == len(page.items) == 1
    summary = page.items[0]
    assert summary.model_dump(mode="json") == detail_summary.model_dump(mode="json")
    assert summary.track_count == detail_tracks.total == len(detail_tracks.items)
    _assert_pages_complete(detail_tracks)

    canonical = _canonical_tracks()
    for track in detail_tracks.items:
        _assert_catalog_track(track, canonical)

    # "Любимая подборка" mirrors the likes page one-to-one.
    likes = LibraryPage.model_validate(load_fixture("likes.json"))
    assert [track.id for track in detail_tracks.items] == [
        track.id for track in likes.items
    ]


def test_album_detail_agrees_with_catalog() -> None:
    detail = _validated("album.json", YandexAlbumDetail)
    canonical = _canonical_tracks()
    assert detail.album.id in _canonical_album_ids()
    assert detail.album.track_count == len(detail.tracks)
    assert detail.tracks, "album detail must list tracks"
    assert len({track.id for track in detail.tracks}) == len(detail.tracks)

    for track in detail.tracks:
        _assert_catalog_track(track, canonical)
        assert track.album is not None
        assert track.album.id == detail.album.id
        assert track.album.title == detail.album.title
        assert track.album.year == detail.album.year

    assert detail.album.artist is not None
    assert all(
        artist.id == detail.album.artist.id for track in detail.tracks for artist in track.artists
    )
    _assert_cover_url(detail.album.cover_url)


def test_artist_detail_agrees_with_catalog() -> None:
    detail = _validated("artist.json", YandexArtistDetail)
    canonical = _canonical_tracks()
    assert detail.artist.id in _canonical_artist_ids()
    assert detail.artist.track_count == len(detail.tracks)
    assert detail.artist.album_count >= 1
    assert detail.tracks, "artist detail must list tracks"
    assert len({track.id for track in detail.tracks}) == len(detail.tracks)

    for track in detail.tracks:
        _assert_catalog_track(track, canonical)
        assert all(artist.id == detail.artist.id for artist in track.artists)
    _assert_cover_url(detail.artist.cover_url)


def test_search_results_reference_known_catalog_entities() -> None:
    results = _validated("search.json", YandexSearchResults)
    canonical = _canonical_tracks()

    for page in (results.tracks, results.albums, results.artists):
        _assert_pages_complete(page)
    assert results.playlists.total == len(results.playlists.items) == 1

    assert results.tracks.items, "search fixture must return tracks"
    for track in results.tracks.items:
        _assert_catalog_track(track, canonical)

    known_albums = _canonical_album_ids()
    for album in results.albums.items:
        assert album.id in known_albums
        _assert_cover_url(album.cover_url)

    known_artists = _canonical_artist_ids()
    for artist in results.artists.items:
        assert artist.id in known_artists
        _assert_cover_url(artist.cover_url)

    playlists = YandexPlaylistPage.model_validate(load_fixture("playlists.json"))
    expected_playlists = [item.model_dump(mode="json") for item in playlists.items]
    assert [item.model_dump(mode="json") for item in results.playlists.items] == (
        expected_playlists
    )


def test_wave_references_liked_catalog_tracks() -> None:
    wave = _validated("wave.json", WaveState)
    canonical = _canonical_tracks()

    assert wave.station == "user:onyourwave"
    assert wave.batch_id
    assert wave.tracks
    assert 0 <= wave.current_index < len(wave.tracks)
    assert len({track.id for track in wave.tracks}) == len(wave.tracks)
    _parse_timestamp(wave.updated_at)

    for track in wave.tracks:
        _assert_catalog_track(track, canonical)
        assert track.liked is True


# --------------------------------------------------------------------------- #
# Stream diagnostics and errors
# --------------------------------------------------------------------------- #


def test_stream_diagnostic_is_not_a_signed_url() -> None:
    payload = load_fixture("stream.json")
    assert set(payload) == {
        "track_id",
        "external_id",
        "codec",
        "bitrate_kbps",
        "preview",
        "expires_at",
    }
    raw = json.dumps(payload).lower()
    assert "http" not in raw, "stream.json must not carry a direct link"
    assert "url" not in raw

    canonical = _canonical_tracks()
    track = canonical[payload["track_id"]]
    assert payload["external_id"] == track["external_id"]
    assert payload["preview"] is track["preview"]
    assert payload["codec"] in {"mp3", "aac"}
    assert 64 <= payload["bitrate_kbps"] <= 320
    _parse_timestamp(payload["expires_at"])


def test_error_fixtures_use_the_api_error_envelope() -> None:
    expected_codes = {
        "error_auth.json": "auth_required",
        "error_region.json": "no_playable_source",
    }

    for name, code in expected_codes.items():
        payload = load_fixture(name)
        assert set(payload) == {"error"}, f"{name} must use the REST error envelope"
        error = payload["error"]
        assert set(error) == {"code", "message", "details"}
        assert error["code"] == code
        assert ERROR_CODE.match(error["code"])
        assert error["message"].strip()
        assert isinstance(error["details"], dict)

    region = load_fixture("error_region.json")["error"]["details"]
    assert region["reason"] == "region_restricted"
    assert region["availability"] == "unavailable"
    assert EXTERNAL_ID.match(region["external_id"])

    auth = load_fixture("error_auth.json")["error"]["details"]
    assert auth["reason"] == "token_missing"
