"""WebSocket snapshot, events, visibility and two-client synchronisation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _receive_until(
    websocket, predicate: Callable[[dict[str, Any]], bool], *, limit: int = 30
) -> dict[str, Any]:
    for _ in range(limit):
        message = websocket.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected event was not received")


def _first_track(client: TestClient) -> dict[str, Any]:
    return client.get("/api/v1/library/tracks").json()["items"][0]


def test_snapshot_on_connect(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        payload = snapshot["payload"]
        assert payload["server"]["mode"] == "mock"
        assert payload["player"]["status"] == "stopped"
        assert payload["queue"]["total"] == 0
        assert len(payload["library"]["shelf"]) == 6
        assert "token" not in str(snapshot).lower()


def test_ping_pong(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "ping"})
        pong = websocket.receive_json()
        assert pong["type"] == "pong"
        assert pong["payload"] == {}


def test_unknown_message_returns_error(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "nonsense"})
        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["payload"]["code"] == "bad_message"


def test_visible_client_receives_position(client: TestClient) -> None:
    track = _first_track(client)
    with client.websocket_connect("/api/v1/ws") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "client.hello", "payload": {"visible": True}})

        response = client.post("/api/v1/player/play", json={"track_id": track["id"]})
        assert response.status_code == 200

        events: list[str] = []
        position_event = _receive_until(
            websocket,
            lambda message: message["type"] == "player.position",
        )
        events.append(position_event["type"])
        assert position_event["payload"]["status"] == "playing"
        assert position_event["payload"]["duration_ms"] == track["duration_ms"]
        assert position_event["payload"]["server_time_ms"] > 0


def test_hidden_client_does_not_receive_position(client: TestClient) -> None:
    track = _first_track(client)
    with client.websocket_connect("/api/v1/ws") as websocket:
        websocket.receive_json()
        # No client.hello: the tab is treated as hidden.

        client.post("/api/v1/player/play", json={"track_id": track["id"]})
        time.sleep(0.6)
        client.post("/api/v1/player/pause")

        types: list[str] = []
        for _ in range(20):
            message = websocket.receive_json()
            types.append(message["type"])
            if (
                message["type"] == "player.state_changed"
                and message["payload"]["state"]["status"] == "paused"
            ):
                break
        assert "player.position" not in types
        assert "player.state_changed" in types


def test_two_clients_sync(client: TestClient) -> None:
    tracks = client.get("/api/v1/library/tracks").json()["items"][:2]
    client.post("/api/v1/queue", json={"track_ids": [t["id"] for t in tracks]})

    with (
        client.websocket_connect("/api/v1/ws") as first,
        client.websocket_connect("/api/v1/ws") as second,
    ):
        first.receive_json()
        second.receive_json()
        for websocket in (first, second):
            websocket.send_json({"type": "client.hello", "payload": {"visible": True}})

        client.post("/api/v1/player/play", json={"track_id": tracks[0]["id"]})

        playing = {"status": "playing"}
        first_state = _receive_until(
            first,
            lambda message: message["type"] == "player.state_changed"
            and message["payload"]["state"]["status"] == playing["status"],
        )
        second_state = _receive_until(
            second,
            lambda message: message["type"] == "player.state_changed"
            and message["payload"]["state"]["status"] == playing["status"],
        )
        assert first_state["payload"]["state"]["track"]["id"] == tracks[0]["id"]
        assert second_state["payload"]["state"]["track"]["id"] == tracks[0]["id"]

        client.post("/api/v1/player/next")
        first_next = _receive_until(
            first,
            lambda message: message["type"] == "player.track_changed"
            and message["payload"]["state"]["track"]["id"] == tracks[1]["id"],
        )
        second_next = _receive_until(
            second,
            lambda message: message["type"] == "player.track_changed"
            and message["payload"]["state"]["track"]["id"] == tracks[1]["id"],
        )
        assert first_next["payload"]["state"]["queue_index"] == 1
        assert second_next["payload"]["state"]["queue_index"] == 1

        queue_event = _receive_until(
            first, lambda message: message["type"] == "queue.updated"
        )
        assert queue_event["payload"]["queue"]["total"] == 2


def test_rejects_foreign_origin(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as error,
        client.websocket_connect(
            "/api/v1/ws", headers={"origin": "http://evil.example"}
        ) as websocket,  # pragma: no cover - close happens before accept
    ):
        websocket.receive_json()
    assert error.value.code == 1008
