"""Shuffle/repeat transport modes and queue-clear semantics.

Drives the real PlayerManager through the mock audio backend: EOF behavior
for repeat one/all and shuffle, manual Next under shuffle, mode persistence
across restart, and the clear-queue endpoint keeping the current track.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def _tracks(client: TestClient) -> list[dict]:
    return client.get("/api/v1/library/tracks").json()["items"]


def _by_title(client: TestClient, title: str) -> dict:
    return next(item for item in _tracks(client) if item["title"] == title)


def _finish_now(client: TestClient, track: dict) -> None:
    client.post(
        "/api/v1/player/seek",
        json={"position_ms": max(track["duration_ms"] - 100, 0)},
    )
    time.sleep(1.2)


def test_mode_endpoint_roundtrip_and_defaults(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        state = client.get("/api/v1/player/state").json()
        assert state["shuffle"] is False
        assert state["repeat"] == "off"

        response = client.post("/api/v1/player/mode", json={"shuffle": True})
        assert response.status_code == 200
        assert response.json()["shuffle"] is True
        assert response.json()["repeat"] == "off"

        response = client.post("/api/v1/player/mode", json={"repeat": "one"})
        assert response.json()["repeat"] == "one"
        assert response.json()["shuffle"] is True

        empty = client.post("/api/v1/player/mode", json={})
        assert empty.status_code == 422


def test_modes_survive_restart(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/v1/player/mode", json={"shuffle": True, "repeat": "all"})

    with TestClient(create_app(make_settings(tmp_path))) as client:
        state = client.get("/api/v1/player/state").json()
        assert state["shuffle"] is True
        assert state["repeat"] == "all"


def test_repeat_one_loops_current_track_on_eof(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], second["id"]]}
        )
        client.post("/api/v1/player/mode", json={"repeat": "one"})
        client.post("/api/v1/player/play", json={"track_id": short["id"]})
        _finish_now(client, short)

        state = client.get("/api/v1/player/state").json()
        assert state["track"]["id"] == short["id"]
        assert state["status"] == "playing"


def test_repeat_one_does_not_duplicate_history_entries(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], second["id"]]}
        )
        client.post("/api/v1/player/mode", json={"repeat": "one"})
        client.post("/api/v1/player/play", json={"track_id": short["id"]})
        _finish_now(client, short)

        history = client.get("/api/v1/history").json()
        plays_of_short = sum(
            1 for item in history["items"] if item["track"]["id"] == short["id"]
        )
        assert plays_of_short == 1


def test_repeat_all_wraps_queue_on_eof(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], second["id"]]}
        )
        client.post("/api/v1/player/mode", json={"repeat": "all"})
        client.post("/api/v1/player/play", json={"track_id": second["id"]})
        _finish_now(client, second)

        state = client.get("/api/v1/player/state").json()
        assert state["track"]["id"] == short["id"]
        assert state["status"] == "playing"


def test_manual_next_wraps_with_repeat_all(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], second["id"]]}
        )
        client.post("/api/v1/player/mode", json={"repeat": "all"})
        client.post("/api/v1/player/play", json={"track_id": second["id"]})

        state = client.post("/api/v1/player/next").json()
        assert state["track"]["id"] == short["id"]


def test_shuffle_next_picks_other_playable_track(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        third = _by_title(client, "Тихий дождь")
        client.post(
            "/api/v1/queue",
            json={"track_ids": [short["id"], second["id"], third["id"]]},
        )
        client.post("/api/v1/player/mode", json={"shuffle": True})
        client.post("/api/v1/player/play", json={"track_id": short["id"]})

        # Deterministic shuffle: always take the first candidate.
        monkeypatch.setattr(
            "app.core.player_manager.random.choice", lambda seq: seq[0]
        )
        state = client.post("/api/v1/player/next").json()
        assert state["track"]["id"] == second["id"]

        # can_next stays honest at the last position while shuffling.
        client.post("/api/v1/player/play", json={"track_id": third["id"]})
        state = client.get("/api/v1/player/state").json()
        assert state["can_next"] is True


def test_can_next_false_at_queue_end_without_modes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], second["id"]]}
        )
        client.post("/api/v1/player/play", json={"track_id": second["id"]})
        state = client.get("/api/v1/player/state").json()
        assert state["can_next"] is False


def test_clear_queue_keeps_current_and_keeps_playing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        third = _by_title(client, "Тихий дождь")
        client.post(
            "/api/v1/queue",
            json={"track_ids": [short["id"], second["id"], third["id"]]},
        )
        client.post("/api/v1/player/play", json={"track_id": short["id"]})

        queue = client.delete("/api/v1/queue").json()
        assert queue["total"] == 1
        assert queue["items"][0]["track"]["id"] == short["id"]

        state = client.get("/api/v1/player/state").json()
        assert state["track"]["id"] == short["id"]
        assert state["status"] == "playing"


def test_clear_queue_without_current_removes_everything(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        short = _by_title(client, "Короткая проверка")
        second = _by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], second["id"]]}
        )
        queue = client.delete("/api/v1/queue").json()
        assert queue["total"] == 0
