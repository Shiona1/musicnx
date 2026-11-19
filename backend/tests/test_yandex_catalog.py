"""Yandex catalog tests: mapper, SQLite normalization, mock adapter (no network)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes import yandex as yandex_routes
from app.core.models import YandexPlaylist
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.errors import AppError, AuthenticationError
from app.integrations.yandex_music import mapper
from app.integrations.yandex_music.catalog import YandexCatalogService, parse_playlist_id
from app.integrations.yandex_music.mock_adapter import MockYandexAdapter
from app.integrations.yandex_music.repository import YandexRepository
from tests.p3_yandex_fixtures import (
    asgi_client,
    fake_album,
    fake_artist,
    fake_track,
    fake_track_short,
    make_router_app,
    seed_online_track,
    stream_ctx,
)

# --------------------------------------------------------------------- mapper


def test_mapper_reports_schema_drift_as_warning() -> None:
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("musicnx.yandex.mapper")
    handler = _Collector()
    logger.addHandler(handler)
    before = mapper.schema_warning_counts().get("Track.id", 0)
    try:
        assert mapper.map_track(SimpleNamespace()) is None
    finally:
        logger.removeHandler(handler)

    # A missing identity is skipped with a warning, never an exception.
    assert mapper.schema_warning_counts()["Track.id"] == before + 1
    assert any("schema mismatch" in record.getMessage() for record in records)


def test_cover_url_resizes_only_yandex_hosts() -> None:
    assert (
        mapper.cover_url("avatars.yandex.net/get-music-content/1/abc/%%", 300)
        == "https://avatars.yandex.net/get-music-content/1/abc/300x300"
    )
    assert (
        mapper.cover_url("https://avatars.yandex.net/get-music-content/1/abc/%%", 1000)
        == "https://avatars.yandex.net/get-music-content/1/abc/1000x1000"
    )
    assert (
        mapper.cover_url("avatars.yandex.net/get-music-content/1/abc/300x300", 64)
        == "https://avatars.yandex.net/get-music-content/1/abc/64x64"
    )
    assert mapper.cover_url("evil.example/cover/%%", 300) is None
    assert mapper.cover_url("avatars.yandex.net/../secret/%%", 300) is None
    assert mapper.cover_url(None) is None


def test_map_track_normalizes_external_id_without_preview_guess() -> None:
    artist = fake_artist(8001, "Artist One")
    album = fake_album(5001, "Album One", year=2024)
    full = mapper.map_track(
        fake_track(1001, "Полный трек", artists=(artist,), albums=(album,))
    )
    assert full is not None
    assert full.external_id == "1001:5001"
    assert full.yandex_id == "1001"
    assert full.album_id == "5001"
    assert full.artists[0].name == "Artist One"
    assert full.preview is False
    assert full.available is True
    assert full.year == 2024

    sample = mapper.map_track(
        fake_track(
            1002,
            "30 секунд",
            artists=(artist,),
            albums=(album,),
            preview_duration_ms=30_000,
        )
    )
    assert sample is not None
    # preview_duration_ms is not an access restriction; DownloadInfo.preview wins.
    assert sample.preview is False

    unavailable = mapper.map_track(
        fake_track(1003, "Нет в регионе", artists=(artist,), albums=(), error="region")
    )
    assert unavailable is not None
    assert unavailable.external_id == "1003:1003"
    assert unavailable.available is False


def test_track_short_ids_preserves_order_and_drops_duplicates() -> None:
    shorts = [
        fake_track_short(1001),
        fake_track_short("1002"),
        fake_track_short(1001),
        SimpleNamespace(id=None, track=SimpleNamespace(id=1003)),
        SimpleNamespace(id=None, track=None),
    ]
    assert mapper.track_short_ids(shorts) == ["1001", "1002", "1003"]


def test_parse_playlist_id_rejects_malformed_values() -> None:
    assert parse_playlist_id("123:456") == ("123", 456)
    with pytest.raises(AppError) as exc_info:
        parse_playlist_id("no-kind")
    assert exc_info.value.code == "invalid_playlist_id"
    assert exc_info.value.status_code == 400


# ----------------------------------------------------------------- repository


async def test_repository_upsert_is_idempotent(db: Database) -> None:
    repository = YandexRepository(db, TrackRepository(db))
    normalized = mapper.map_track(
        fake_track(
            1001,
            "Трек",
            artists=(fake_artist(8001, "Artist"),),
            albums=(fake_album(5001, "Album", year=2024),),
        )
    )
    assert normalized is not None

    first = await repository.upsert_track(normalized)
    second = await repository.upsert_track(normalized)
    assert first == second

    track = await repository.get_track_by_external(normalized.external_id)
    assert track is not None
    assert track.id == first
    assert track.source == "yandex"
    assert track.external_id == "1001:5001"
    assert track.has_online_source is True
    assert track.source_unavailable is False
    assert track.availability == "online"
    assert await repository.get_external_id(first) == "1001:5001"

    listed = await repository.list_tracks([first])
    assert listed == [track]


async def test_repository_refreshes_fields_and_marks_unavailable(db: Database) -> None:
    repository = YandexRepository(db, TrackRepository(db))
    base = mapper.map_track(
        fake_track(1001, "Старое имя", albums=(fake_album(5001, "Album"),))
    )
    assert base is not None
    track_id = await repository.upsert_track(base)

    renamed = mapper.map_track(
        fake_track(
            1001,
            "Новое имя",
            albums=(fake_album(5001, "Album"),),
            available=False,
        )
    )
    assert renamed is not None
    assert await repository.upsert_track(renamed) == track_id
    track = await repository.get_track_by_external(base.external_id)
    assert track is not None
    assert track.title == "Новое имя"
    assert track.source_unavailable is True
    assert track.availability == "unavailable"


async def test_repository_migrates_external_id_when_album_changes(db: Database) -> None:
    repository = YandexRepository(db, TrackRepository(db))
    first = mapper.map_track(
        fake_track(1001, "Трек", albums=(fake_album(5001, "Album"),))
    )
    second = mapper.map_track(
        fake_track(1001, "Трек", albums=(fake_album(5002, "Re-release"),))
    )
    assert first is not None and second is not None
    track_id = await repository.upsert_track(first)

    # UNIQUE(track_id, source_id) allows one Yandex row per track: migrate it.
    assert await repository.upsert_track(second) == track_id
    assert await repository.get_external_id(track_id) == "1001:5002"
    assert await repository.get_track_by_external("1001:5001") is None


# --------------------------------------------------------------- mock adapter


async def test_mock_adapter_catalog_returns_internal_tracks(db: Database) -> None:
    repository = YandexRepository(db, TrackRepository(db))
    adapter = MockYandexAdapter(repository)

    page = await adapter.likes(limit=2, offset=0)
    assert page.total == 4
    assert len(page.items) == 2
    assert all(item.liked is True for item in page.items)
    assert all(item.source == "yandex" for item in page.items)
    assert all(item.external_id for item in page.items)
    assert all(item.cover_url for item in page.items)
    assert await TrackRepository(db).get(page.items[0].id) is not None

    playlists = await adapter.playlists()
    assert playlists.total == 1
    assert {item.kind for item in playlists.items} == {7}
    assert all(isinstance(item, YandexPlaylist) for item in playlists.items)

    dto, tracks = await adapter.playlist("1001:7", limit=2, offset=1)
    assert dto.id == "1001:7"
    assert dto.kind == 7
    assert dto.title == "Любимая подборка"
    assert dto.track_count == 4
    assert dto.owner == "musicnx-demo"
    assert tracks.total == 4
    assert len(tracks.items) == 2
    assert [item.title for item in tracks.items] == ["Северное сияние", "Городские огни"]

    album = await adapter.album("501")
    assert album.album.title == "Midnight Sessions"
    assert album.album.year == 2024
    assert album.album.track_count == 2
    assert album.album.artist is not None
    assert album.album.artist.name == "Тестовый Оркестр"
    assert len(album.tracks) == 2

    artist = await adapter.artist("301")
    assert artist.artist.name == "Тестовый Оркестр"
    assert artist.artist.track_count == 2
    assert artist.artist.album_count == 1
    assert len(artist.tracks) == 2

    results = await adapter.search("Полночный", type_="all", limit=25, offset=0)
    assert results.tracks.total == 1
    assert results.tracks.items[0].title == "Полночный эфир"

    # The shared fixture catalog answers q=тест with 2 tracks, 1 album,
    # 1 artist and 1 playlist; the mock adapter must agree with it.
    fixture_query = await adapter.search("тест", type_="all", limit=25, offset=0)
    assert fixture_query.tracks.total == 2
    assert fixture_query.albums.total == 1
    assert fixture_query.artists.total == 1
    assert fixture_query.playlists.total == 1

    with pytest.raises(AppError):
        await adapter.search("   ", type_="all", limit=25, offset=0)


async def test_mock_adapter_auth_flow_is_in_memory(db: Database) -> None:
    adapter = MockYandexAdapter(YandexRepository(db, TrackRepository(db)))

    assert (await adapter.auth_status()).authenticated is False

    start = await adapter.start_device_auth()
    assert start.user_code
    assert start.expires_in == 300

    pending = await adapter.device_auth_status(start.flow_id)
    assert pending.state == "pending"
    authorized = await adapter.device_auth_status(start.flow_id)
    assert authorized.state == "authorized"
    assert (await adapter.auth_status()).authenticated is True

    manual = await adapter.set_manual_token("mock-token")
    assert manual.authenticated is True
    assert manual.source == "manual"

    await adapter.logout()
    assert (await adapter.auth_status()).authenticated is False

    with pytest.raises(AuthenticationError):
        await adapter.set_manual_token("  ")


async def test_mock_adapter_delegates_auth_to_flow_a_manager(
    db: Database, settings: Any
) -> None:
    from app.core.event_bus import EventBus
    from app.integrations.yandex_music.auth import MockYandexAuthManager

    manager = MockYandexAuthManager(settings.yandex, EventBus())
    adapter = MockYandexAdapter(
        YandexRepository(db, TrackRepository(db)), auth=manager
    )

    assert (await adapter.auth_status()).authenticated is False
    start = await adapter.start_device_auth()
    assert (await adapter.device_auth_status(start.flow_id)).state == "pending"
    assert (await adapter.device_auth_status(start.flow_id)).state == "authorized"
    assert (await adapter.set_manual_token("mock-token")).source == "manual"
    await adapter.logout()
    assert (await adapter.auth_status()).authenticated is False


async def test_mock_adapter_stream_info_is_local(db: Database) -> None:
    adapter = MockYandexAdapter(YandexRepository(db, TrackRepository(db)))
    info = await adapter.stream_info("9001:501")
    assert info.url == "mock://stream/9001"
    assert info.codec == "mp3"
    assert info.preview is False

    preview = await adapter.stream_info("9004:502")
    assert preview.preview is True


# -------------------------------------------------------------- pagination


class _FakeCatalogClient:
    """Only the calls :class:`YandexCatalogService.likes` makes."""

    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        self._shorts = [fake_track_short(1000 + index) for index in range(5)]
        self._tracks = {
            str(1000 + index): fake_track(
                1000 + index,
                f"Трек {index}",
                artists=(fake_artist(8001, "Artist"),),
                albums=(fake_album(5001, "Album"),),
            )
            for index in range(5)
        }

    async def likes(self) -> list[SimpleNamespace]:
        return self._shorts

    async def tracks(self, track_ids: list[str]) -> list[SimpleNamespace]:
        self.requests.append(list(track_ids))
        return [self._tracks[track_id] for track_id in track_ids if track_id in self._tracks]


async def test_catalog_likes_paginates_and_persists_positions(db: Database) -> None:
    repository = YandexRepository(db, TrackRepository(db))
    client = _FakeCatalogClient()
    service = YandexCatalogService(client, repository)  # type: ignore[arg-type]

    page = await service.likes(limit=2, offset=2)
    assert page.total == 5
    assert page.limit == 2
    assert page.offset == 2
    assert [track.title for track in page.items] == ["Трек 2", "Трек 3"]
    assert client.requests == [["1002", "1003"]]
    assert all(track.liked is True for track in page.items)
    assert all(track.external_id for track in page.items)

    playlist_row = await db.fetchone(
        "SELECT id FROM playlists WHERE external_id = 'likes'"
    )
    assert playlist_row is not None
    stored = await repository.playlist_track_ids(int(playlist_row["id"]))
    assert stored == [track.id for track in page.items]

    # A second page merges instead of replacing the first one.
    first_page = await service.likes(limit=2, offset=0)
    stored = await repository.playlist_track_ids(int(playlist_row["id"]))
    assert stored == [track.id for track in first_page.items] + [
        track.id for track in page.items
    ]


async def test_online_source_is_visible_to_local_track_reads(db: Database) -> None:
    track_id = await seed_online_track(db)
    track = await TrackRepository(db).get(track_id)
    assert track is not None
    assert track.availability == "online"
    # The shared repository does not know Yandex provenance; the Yandex one does.
    yandex_repository = YandexRepository(db, TrackRepository(db))
    enriched = await yandex_repository.get_track_by_external("1001:5001")
    assert enriched is not None
    assert enriched.source == "yandex"
    assert enriched.external_id == "1001:5001"


# --------------------------------------------------------------------- routes


class _AuthRequiredAdapter:
    """Adapter whose catalog calls report a missing Yandex token."""

    async def likes(self, *, limit: int, offset: int):
        raise AuthenticationError("Yandex Music authorization required")


async def test_yandex_router_serves_catalog_and_wave(db: Database, settings) -> None:
    repository = YandexRepository(db, TrackRepository(db))
    adapter = MockYandexAdapter(repository)
    ctx = stream_ctx(settings, db, yandex_adapter=adapter)
    app = make_router_app(yandex_routes.router, settings, ctx)

    async with asgi_client(app) as client:
        likes = await client.get(
            "/api/v1/yandex/likes", params={"limit": 2, "offset": 0}
        )
        assert likes.status_code == 200
        payload = likes.json()
        assert payload["total"] == 4
        assert len(payload["items"]) == 2
        assert payload["items"][0]["source"] == "yandex"
        assert payload["items"][0]["cover_url"]

        playlists = await client.get("/api/v1/yandex/playlists")
        assert playlists.status_code == 200
        assert playlists.json()["items"][0]["id"] == "1001:7"

        playlist = await client.get(
            "/api/v1/yandex/playlists/1001:7", params={"limit": 2, "offset": 1}
        )
        assert playlist.status_code == 200
        body = playlist.json()
        assert body["playlist"]["kind"] == 7
        assert body["tracks"]["total"] == 4
        assert [item["title"] for item in body["tracks"]["items"]] == [
            "Северное сияние",
            "Городские огни",
        ]

        album = await client.get("/api/v1/yandex/album/501")
        assert album.status_code == 200
        assert album.json()["album"]["title"] == "Midnight Sessions"

        artist = await client.get("/api/v1/yandex/artist/301")
        assert artist.status_code == 200
        assert artist.json()["artist"]["name"] == "Тестовый Оркестр"

        search = await client.get(
            "/api/v1/yandex/search", params={"q": "тест", "type": "all"}
        )
        assert search.status_code == 200
        assert search.json()["tracks"]["total"] == 2

        invalid = await client.get("/api/v1/yandex/playlists/not-a-playlist")
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_playlist_id"

        wave = await client.post("/api/v1/yandex/wave/start")
        assert wave.status_code == 200
        wave_payload = wave.json()
        assert wave_payload["station"] == "user:onyourwave"
        assert len(wave_payload["tracks"]) == 3

        feedback = await client.post(
            "/api/v1/yandex/wave/feedback",
            json={"action": "skip", "track_id": wave_payload["tracks"][0]["id"]},
        )
        assert feedback.status_code == 200
        assert feedback.json()["tracks"]

        bad_action = await client.post(
            "/api/v1/yandex/wave/feedback",
            json={"action": "dance", "track_id": wave_payload["tracks"][0]["id"]},
        )
        assert bad_action.status_code == 422


async def test_yandex_router_maps_missing_auth_to_401(db: Database, settings) -> None:
    ctx = stream_ctx(settings, db, yandex_adapter=_AuthRequiredAdapter())
    app = make_router_app(yandex_routes.router, settings, ctx)
    async with asgi_client(app) as client:
        response = await client.get("/api/v1/yandex/likes")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


async def test_yandex_router_without_adapter_is_503(db: Database, settings) -> None:
    app = make_router_app(yandex_routes.router, settings, stream_ctx(settings, db))
    async with asgi_client(app) as client:
        response = await client.get("/api/v1/yandex/likes")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "yandex_not_configured"


async def test_yandex_cover_proxy_rejects_foreign_hosts_and_sizes(
    db: Database, settings
) -> None:
    app = make_router_app(yandex_routes.router, settings, stream_ctx(settings, db))
    async with asgi_client(app) as client:
        foreign = await client.get(
            "/api/v1/yandex/cover",
            params={"uri": "https://evil.example/get-music-content/%%"},
        )
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "cover_forbidden"

        bad_size = await client.get(
            "/api/v1/yandex/cover",
            params={
                "uri": "https://avatars.yandex.net/get-music-content/1/a/%%",
                "size": 128,
            },
        )
        assert bad_size.status_code == 422
        assert bad_size.json()["error"]["code"] == "invalid_cover_size"

        traversal = await client.get(
            "/api/v1/yandex/cover",
            params={"uri": "https://avatars.yandex.net/../secret/%%"},
        )
        assert traversal.status_code == 422
        assert traversal.json()["error"]["code"] == "invalid_cover_uri"


async def test_yandex_cover_proxy_serves_cached_image_without_network(
    db: Database, settings
) -> None:
    # Pre-populate the cache so this stays a pure filesystem test (no network).
    import hashlib

    directory = settings.paths.cache / "yandex_covers"
    directory.mkdir(parents=True, exist_ok=True)
    path = "/get-music-content/1/a/300x300"
    key = hashlib.sha256(f"300:{path}".encode()).hexdigest()
    image = b"\xff\xd8\xff\xe0cached-cover"
    (directory / f"{key}.jpg").write_bytes(image)

    app = make_router_app(yandex_routes.router, settings, stream_ctx(settings, db))
    async with asgi_client(app) as client:
        response = await client.get(
            "/api/v1/yandex/cover",
            params={"uri": f"avatars.yandex.net{path}", "size": 300},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"].startswith("public, max-age=")
    assert response.content == image
