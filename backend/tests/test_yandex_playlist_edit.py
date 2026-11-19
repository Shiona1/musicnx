"""Yandex playlist editing routes (fake adapter, no network)."""

from __future__ import annotations

from pathlib import Path

from app.core.models import (
    ArtistSummary,
    LibraryPage,
    YandexArtistDetail,
    YandexPlaylist,
)
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"


def _playlist(title: str = "Mix") -> YandexPlaylist:
    return YandexPlaylist(
        id="42:7", kind=7, title=title, track_count=1, cover_url=None, owner="42"
    )


class FakeEditingAdapter:
    mode = "real"

    async def likes(self, *, limit: int, offset: int) -> LibraryPage:
        return LibraryPage(items=[], total=0, limit=limit, offset=offset)

    async def playlists(self):
        from app.core.models import YandexPlaylistPage

        return YandexPlaylistPage(items=[_playlist()], total=1)

    async def playlist(self, playlist_id: str, *, limit: int, offset: int):
        return _playlist(), LibraryPage(items=[], total=0, limit=limit, offset=offset)

    async def artist(self, artist_id: str):
        return YandexArtistDetail(
            artist=ArtistSummary(id=1, name="Artist"), tracks=[]
        )

    async def artist_tracks(self, artist_id: str, *, limit: int, offset: int):
        return LibraryPage(items=[], total=100, limit=limit, offset=offset)

    async def create_playlist(self, title: str, *, visibility: str = "private"):
        self.created = (title, visibility)
        return _playlist(title)

    async def rename_playlist(self, playlist_id: str, title: str):
        self.renamed = (playlist_id, title)
        return _playlist(title)

    async def delete_playlist(self, playlist_id: str) -> None:
        self.deleted = playlist_id

    async def add_playlist_track(self, playlist_id: str, track_id: int, *, at: int = 0):
        self.added = (playlist_id, track_id, at)
        return _playlist()

    async def remove_playlist_track(self, playlist_id: str, position: int):
        self.removed = (playlist_id, position)
        return _playlist()

    async def close(self) -> None:
        return None


def test_playlist_editing_routes_use_adapter(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        adapter = FakeEditingAdapter()
        app.state.ctx.yandex_adapter = adapter

        created = client.post(
            f"{API}/yandex/playlists",
            json={"title": "Road", "visibility": "private"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["title"] == "Road"
        assert adapter.created == ("Road", "private")

        renamed = client.patch(
            f"{API}/yandex/playlists/42%3A7", json={"title": "Night"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "Night"
        assert adapter.renamed == ("42:7", "Night")

        added = client.post(
            f"{API}/yandex/playlists/42%3A7/tracks",
            json={"track_id": 5, "at": 0},
        )
        assert added.status_code == 200, added.text
        assert adapter.added == ("42:7", 5, 0)

        removed = client.delete(
            f"{API}/yandex/playlists/42%3A7/tracks", params={"position": 2}
        )
        assert removed.status_code == 200, removed.text
        assert adapter.removed == ("42:7", 2)

        deleted = client.delete(f"{API}/yandex/playlists/42%3A7")
        assert deleted.status_code == 204
        assert adapter.deleted == "42:7"


def test_playlist_editing_unavailable_on_mock_adapter(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.post(
            f"{API}/yandex/playlists", json={"title": "Nope"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "yandex_playlist_editing_unavailable"


def test_artist_tracks_pagination_route(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        app.state.ctx.yandex_adapter = FakeEditingAdapter()
        response = client.get(
            f"{API}/yandex/artist/1/tracks", params={"limit": 25, "offset": 50}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 100
    assert body["limit"] == 25
    assert body["offset"] == 50
