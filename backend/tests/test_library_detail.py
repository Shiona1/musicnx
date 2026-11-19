"""Album/artist detail endpoints back the library detail pages.

The mock catalog must expose the same data shape the UI renders: one album
with its ordered track list, one artist with their tracks, honest 404s.
"""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def test_album_detail_lists_its_tracks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        albums = client.get("/api/v1/library/albums").json()["items"]
        assert albums, "mock catalog must seed at least one album"
        album = albums[0]

        response = client.get(f"/api/v1/library/albums/{album['id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["album"]["id"] == album["id"]
        assert len(body["tracks"]) == album["track_count"]


def test_artist_detail_lists_their_tracks(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        artists = client.get("/api/v1/library/artists").json()["items"]
        assert artists, "mock catalog must seed at least one artist"
        artist = next(a for a in artists if a["track_count"] > 0)

        response = client.get(f"/api/v1/library/artists/{artist['id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["artist"]["id"] == artist["id"]
        assert len(body["tracks"]) == artist["track_count"]


def test_detail_unknown_ids_return_404(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/library/albums/99999").status_code == 404
        assert client.get("/api/v1/library/artists/99999").status_code == 404
