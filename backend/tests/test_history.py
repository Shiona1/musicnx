"""Listening history API: newest-first page and explicit clear."""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path)))


def test_play_is_recorded_and_history_can_be_cleared(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        played = client.post(f"{API}/player/play", json={"track_id": 1})
        assert played.status_code == 200, played.text

        page = client.get(f"{API}/history")
        assert page.status_code == 200
        body = page.json()
        assert body["total"] >= 1
        assert body["items"][0]["track"]["id"] == 1
        assert body["items"][0]["played_at"]

        cleared = client.delete(f"{API}/history")
        assert cleared.status_code == 200
        assert cleared.json()["deleted"] >= 0

        empty = client.get(f"{API}/history").json()
        assert empty["total"] == 0
        assert empty["items"] == []


def test_history_pagination_contract(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for _ in range(3):
            assert client.post(f"{API}/player/play", json={"track_id": 1}).status_code == 200

        first = client.get(f"{API}/history", params={"limit": 2, "offset": 0}).json()
        second = client.get(f"{API}/history", params={"limit": 2, "offset": 2}).json()
        assert first["limit"] == 2 and first["offset"] == 0
        assert len(first["items"]) == 2
        assert second["offset"] == 2
        assert first["items"][0]["id"] != first["items"][1]["id"]
