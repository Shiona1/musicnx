"""Library removal: metadata delete by default, explicit file delete optional."""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path)))


def test_delete_track_removes_it_from_the_library(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        before = client.get(f"{API}/library/tracks", params={"limit": 5}).json()
        assert before["total"] >= 1
        track_id = before["items"][0]["id"]

        deleted = client.delete(f"{API}/library/tracks/{track_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True
        assert deleted.json()["files_deleted"] == []

        after = client.get(f"{API}/library/tracks", params={"limit": 5}).json()
        assert all(item["id"] != track_id for item in after["items"])


def test_delete_unknown_track_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.delete(f"{API}/library/tracks/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_soft_delete_can_be_restored(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        track_id = client.get(f"{API}/library/tracks", params={"limit": 1}).json()["items"][0]["id"]
        assert client.delete(f"{API}/library/tracks/{track_id}").status_code == 200
        restored = client.post(f"{API}/library/tracks/{track_id}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["id"] == track_id
        ids = {
            item["id"]
            for item in client.get(f"{API}/library/tracks", params={"limit": 50}).json()["items"]
        }
        assert track_id in ids

        again = client.post(f"{API}/library/tracks/{track_id}/restore")
        assert again.status_code == 404


def test_track_metadata_can_be_edited(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        track_id = client.get(f"{API}/library/tracks", params={"limit": 1}).json()["items"][0]["id"]
        response = client.patch(
            f"{API}/library/tracks/{track_id}",
            json={
                "title": "Renamed Track",
                "artists": ["New Artist"],
                "album": "New Album",
                "year": 2026,
                "genre": "Test",
                "track_number": 2,
                "disc_number": 1,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["track"]["title"] == "Renamed Track"
        assert body["track"]["artists"][0]["name"] == "New Artist"
        assert body["track"]["album"]["title"] == "New Album"
        assert body["track"]["album"]["year"] == 2026
        assert body["file_tags"] == "skipped"

        found = client.get(f"{API}/library/tracks", params={"search": "Renamed Track"})
        assert found.status_code == 200
        assert any(item["id"] == track_id for item in found.json()["items"])
