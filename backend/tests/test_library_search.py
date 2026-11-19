"""P2 FTS search, albums/artists endpoints and pagination."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


@contextmanager
def _imported_client(tmp_path: Path, root: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(make_settings(tmp_path, seed_catalog=False, import_roots=[root]))
    ) as client:
        client.post("/api/v1/library/import")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = client.get("/api/v1/library/import/status").json()["job"]
            if job and job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        yield client


def test_fts_search_and_pagination(tmp_path: Path, library_root: Path) -> None:
    with _imported_client(tmp_path, library_root) as client:
        shared = client.get("/api/v1/library/tracks", params={"search": "Shared"}).json()
        assert shared["total"] == 2
        assert all(item["title"] == "Shared Song" for item in shared["items"])

        by_artist = client.get("/api/v1/library/tracks", params={"search": "Test"}).json()
        assert by_artist["total"] == 2

        unicode = client.get(
            "/api/v1/library/tracks", params={"search": "Unicode"}
        ).json()
        assert unicode["total"] == 1
        assert unicode["items"][0]["title"] == "Unicode Песня"

        cyrillic = client.get(
            "/api/v1/library/tracks", params={"search": "Песня"}
        ).json()
        assert cyrillic["total"] == 1

        empty = client.get("/api/v1/library/tracks", params={"search": "zzz-none"}).json()
        assert empty["total"] == 0

        page = client.get(
            "/api/v1/library/tracks", params={"limit": 2, "offset": 2}
        ).json()
        assert len(page["items"]) == 2
        assert page["total"] == 4


def test_albums_and_artists_endpoints(tmp_path: Path, library_root: Path) -> None:
    with _imported_client(tmp_path, library_root) as client:
        albums = client.get("/api/v1/library/albums").json()
        assert albums["total"] == 4
        other = client.get(
            "/api/v1/library/albums", params={"search": "Other"}
        ).json()
        assert other["total"] == 1
        assert other["items"][0]["title"] == "Other Album"
        assert other["items"][0]["track_count"] == 1
        assert other["items"][0]["artist"]["name"] == "Test Artist"
        assert other["items"][0]["cover_url"]

        artists = client.get("/api/v1/library/artists").json()
        assert artists["total"] == 3
        test_artist = client.get(
            "/api/v1/library/artists", params={"search": "Test Artist"}
        ).json()
        assert test_artist["total"] == 1
        assert test_artist["items"][0]["track_count"] == 2

        search = client.get("/api/v1/library/search", params={"q": "Shared"}).json()
        assert search["tracks"]["total"] == 2
        assert search["albums"]["total"] == 0
        assert search["artists"]["total"] == 0

        assert client.get("/api/v1/library/search", params={"q": ""}).status_code == 422


def test_seeded_mock_catalog_is_searchable(client: TestClient) -> None:
    payload = client.get(
        "/api/v1/library/tracks", params={"search": "Ночной"}
    ).json()
    assert payload["total"] == 1
    assert payload["items"][0]["title"] == "Ночной эфир"
