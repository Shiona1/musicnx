"""Player REST endpoints against the mock backend."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _track_ids(client: TestClient) -> list[int]:
    return [item["id"] for item in client.get("/api/v1/library/tracks").json()["items"]]


def test_initial_state_is_stopped(client: TestClient) -> None:
    state = client.get("/api/v1/player/state").json()
    assert state["status"] == "stopped"
    assert state["track"] is None
    assert state["volume"] == 70
    assert state["zone_id"] == "local"
    assert state["can_pause"] is False


def test_play_without_queue_returns_conflict(client: TestClient) -> None:
    response = client.post("/api/v1/player/play")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "queue_empty"


def test_play_unknown_track(client: TestClient) -> None:
    response = client.post("/api/v1/player/play", json={"track_id": 99999})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_play_by_track_id_adds_to_queue(client: TestClient) -> None:
    track_id = _track_ids(client)[0]
    response = client.post("/api/v1/player/play", json={"track_id": track_id})
    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "playing"
    assert state["track"]["id"] == track_id
    assert state["queue_index"] == 0
    assert state["can_pause"] is True

    queue = client.get("/api/v1/queue").json()
    assert queue["total"] == 1
    assert queue["items"][0]["track"]["id"] == track_id


def test_pause_resume_and_stop(client: TestClient) -> None:
    track_id = _track_ids(client)[0]
    client.post("/api/v1/player/play", json={"track_id": track_id})

    paused = client.post("/api/v1/player/pause").json()
    assert paused["status"] == "paused"
    position_at_pause = paused["position_ms"]

    resumed = client.post("/api/v1/player/play").json()
    assert resumed["status"] == "playing"
    assert resumed["position_ms"] >= position_at_pause

    stopped = client.post("/api/v1/player/stop").json()
    assert stopped["status"] == "stopped"
    assert stopped["position_ms"] == 0
    assert stopped["track"]["id"] == track_id

    # Pausing or stopping twice is idempotent.
    assert client.post("/api/v1/player/stop").json()["status"] == "stopped"
    assert client.post("/api/v1/player/pause").json()["status"] == "stopped"


def test_seek_clamps_to_duration(client: TestClient) -> None:
    track = client.get("/api/v1/library/tracks").json()["items"][0]
    client.post("/api/v1/player/play", json={"track_id": track["id"]})

    state = client.post("/api/v1/player/seek", json={"position_ms": 12_345}).json()
    assert state["position_ms"] == 12_345

    state = client.post(
        "/api/v1/player/seek", json={"position_ms": 10_000_000}
    ).json()
    assert state["position_ms"] == track["duration_ms"]

    negative = client.post("/api/v1/player/seek", json={"position_ms": -1})
    assert negative.status_code == 422


def test_seek_without_track_conflicts(client: TestClient) -> None:
    response = client.post("/api/v1/player/seek", json={"position_ms": 1000})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "nothing_to_seek"


def test_next_and_previous(client: TestClient) -> None:
    ids = _track_ids(client)
    client.post("/api/v1/queue", json={"track_ids": ids[:2]})
    client.post("/api/v1/player/play", json={"track_id": ids[0]})

    next_state = client.post("/api/v1/player/next").json()
    assert next_state["queue_index"] == 1
    assert next_state["track"]["id"] == ids[1]

    previous = client.post("/api/v1/player/previous").json()
    assert previous["queue_index"] == 0
    assert previous["track"]["id"] == ids[0]

    # At the first track, previous seeks to 0 instead of stopping.
    client.post("/api/v1/player/seek", json={"position_ms": 5000})
    previous = client.post("/api/v1/player/previous").json()
    assert previous["queue_index"] == 0
    assert previous["position_ms"] == 0

    last = client.post("/api/v1/player/next").json()
    assert last["queue_index"] == 1
    end = client.post("/api/v1/player/next").json()
    assert end["status"] == "stopped"


def test_volume_is_validated_and_persisted(client: TestClient) -> None:
    updated = client.post("/api/v1/player/volume", json={"volume": 33}).json()
    assert updated["volume"] == 33

    settings_value = client.get("/api/v1/system/status").json()["player"]["volume"]
    assert settings_value == 33

    assert client.post("/api/v1/player/volume", json={"volume": 101}).status_code == 422
    assert client.post("/api/v1/player/volume", json={"volume": -1}).status_code == 422
