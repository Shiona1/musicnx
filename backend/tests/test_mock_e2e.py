"""End-to-end mock scenario with no token, no mpv and no Pi."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient


def test_full_mock_playback_flow(client: TestClient) -> None:
    tracks = client.get("/api/v1/library/tracks").json()["items"]
    first, second = tracks[0], tracks[1]

    client.post(
        "/api/v1/queue", json={"track_ids": [first["id"], second["id"]]}
    ).raise_for_status()
    played = client.post("/api/v1/player/play", json={"track_id": first["id"]})
    assert played.status_code == 200
    assert played.json()["status"] == "playing"

    time.sleep(0.7)
    advanced = client.get("/api/v1/player/state").json()
    assert advanced["position_ms"] > 400
    assert advanced["duration_ms"] == first["duration_ms"]

    paused = client.post("/api/v1/player/pause").json()
    frozen = paused["position_ms"]
    time.sleep(0.4)
    still = client.get("/api/v1/player/state").json()
    assert abs(still["position_ms"] - frozen) < 100

    client.post("/api/v1/player/play")
    seeking = client.post("/api/v1/player/seek", json={"position_ms": 2000}).json()
    assert seeking["position_ms"] == 2000

    next_state = client.post("/api/v1/player/next").json()
    assert next_state["track"]["id"] == second["id"]
    assert next_state["queue_index"] == 1

    volume = client.post("/api/v1/player/volume", json={"volume": 12}).json()
    assert volume["volume"] == 12

    queue = client.get("/api/v1/queue").json()
    assert queue["current_item_id"] == next_state["queue_item_id"]
    assert queue["total"] == 2

    stopped = client.post("/api/v1/player/stop").json()
    assert stopped["status"] == "stopped"
    assert stopped["position_ms"] == 0


def test_play_by_track_id_twice_does_not_duplicate_queue(client: TestClient) -> None:
    track = client.get("/api/v1/library/tracks").json()["items"][0]
    client.post("/api/v1/player/play", json={"track_id": track["id"]})
    client.post("/api/v1/player/play", json={"track_id": track["id"]})
    assert client.get("/api/v1/queue").json()["total"] == 1
