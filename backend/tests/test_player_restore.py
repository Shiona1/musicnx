"""State restore, position debounce and EOF/autoplay behaviour."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def _track_by_title(client: TestClient, title: str) -> dict:
    items = client.get("/api/v1/library/tracks").json()["items"]
    return next(item for item in items if item["title"] == title)


def test_position_is_not_written_every_tick(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        track = _track_by_title(client, "Ночной эфир")
        client.post("/api/v1/player/play", json={"track_id": track["id"]})
        time.sleep(0.8)

        live = client.get("/api/v1/player/state").json()
        assert live["position_ms"] > 300

        with sqlite3.connect(settings.paths.database) as connection:
            stored = connection.execute(
                "SELECT position_ms FROM playback_state WHERE id = 1"
            ).fetchone()
        assert stored is not None
        assert stored[0] == 0, "position must be persisted only with debounce"

        paused = client.post("/api/v1/player/pause").json()
        assert paused["position_ms"] > 0
        with sqlite3.connect(settings.paths.database) as connection:
            stored = connection.execute(
                "SELECT position_ms, status FROM playback_state WHERE id = 1"
            ).fetchone()
        assert stored is not None
        assert stored[0] > 0
        assert stored[1] == "paused"


def test_restore_after_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    track_id = None
    saved_position = 0

    with TestClient(create_app(settings)) as client:
        track = _track_by_title(client, "Северное сияние")
        track_id = track["id"]
        client.post("/api/v1/player/play", json={"track_id": track_id})
        time.sleep(0.7)
        paused = client.post("/api/v1/player/pause").json()
        saved_position = paused["position_ms"]
        assert saved_position > 0

    with TestClient(create_app(settings)) as client:
        state = client.get("/api/v1/player/state").json()
        assert state["status"] == "paused"
        assert state["track"]["id"] == track_id
        assert state["position_ms"] == saved_position
        assert client.get("/api/v1/queue").json()["total"] == 1

        resumed = client.post("/api/v1/player/play").json()
        assert resumed["status"] == "playing"
        assert resumed["position_ms"] >= saved_position


def test_playing_is_restored_as_paused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        track = _track_by_title(client, "Городские огни")
        client.post("/api/v1/player/play", json={"track_id": track["id"]})
        time.sleep(0.5)

    with TestClient(create_app(settings)) as client:
        state = client.get("/api/v1/player/state").json()
        assert state["status"] == "paused"
        assert state["track"]["title"] == "Городские огни"


def test_autoplay_advances_on_eof(client: TestClient) -> None:
    short = _track_by_title(client, "Короткая проверка")
    next_track = _track_by_title(client, "Ночной эфир")
    client.post(
        "/api/v1/queue", json={"track_ids": [short["id"], next_track["id"]]}
    )
    client.post("/api/v1/player/play", json={"track_id": short["id"]})
    client.post("/api/v1/player/seek", json={"position_ms": short["duration_ms"] - 100})
    time.sleep(1.0)

    state = client.get("/api/v1/player/state").json()
    assert state["track"]["id"] == next_track["id"]
    assert state["queue_index"] == 1
    assert state["status"] == "playing"


def test_eof_at_end_stops(client: TestClient) -> None:
    short = _track_by_title(client, "Короткая проверка")
    client.post("/api/v1/queue", json={"track_ids": [short["id"]]})
    client.post("/api/v1/player/play", json={"track_id": short["id"]})
    client.post("/api/v1/player/seek", json={"position_ms": short["duration_ms"] - 100})
    time.sleep(1.0)

    state = client.get("/api/v1/player/state").json()
    assert state["status"] == "stopped"
    assert state["position_ms"] == 0
