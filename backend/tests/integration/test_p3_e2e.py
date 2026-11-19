"""P3 end-to-end draft: Yandex auth, catalog, playback, wave, stream, two tabs.

Everything runs in mock mode: no network, no Yandex token, no mpv, no Pi.
The scenarios target the routes documented in docs/API.md §6 and assume the
integrator has wired ``app/api/routes/yandex.py`` to a mock implementation of
``app.integrations.yandex_music.base.YandexAdapter``.

The module-level ``pytestmark`` is the only integration gate; remove it once
the Yandex router and the mock adapter are registered in ``app.main``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"
TOKEN_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_")
DEVICE_POLL_TIMEOUT_S = 5.0

# Integration gate: delete the next line after app.main registers the router.


@pytest.fixture
def p3_client(tmp_path: Path) -> Iterator[TestClient]:
    """Mock-mode app with a fresh seeded database per test."""

    app = create_app(make_settings(tmp_path, mode="mock"))
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _assert_no_token_material(text: str) -> None:
    lowered = text.lower()
    for marker in TOKEN_MARKERS:
        assert marker not in lowered, f"response leaks {marker!r}"


def _receive_until(
    websocket: Any, predicate: Callable[[dict[str, Any]], bool], *, limit: int = 40
) -> dict[str, Any]:
    for _ in range(limit):
        message = websocket.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected event was not received")


def _start_device_flow(client: TestClient) -> dict[str, Any]:
    response = client.post(f"{API}/yandex/auth/device/start")
    assert response.status_code == 200, response.text
    _assert_no_token_material(response.text)
    payload = response.json()
    assert payload["flow_id"]
    assert payload["user_code"]
    assert payload["verification_url"].startswith("https://")
    assert payload["expires_in"] > 0
    assert payload["interval"] > 0
    assert "device_code" not in response.text, "device_code must stay server-side"
    return payload


def _wait_for_device_authorization(client: TestClient, flow_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + DEVICE_POLL_TIMEOUT_S
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"{API}/yandex/auth/device/status", params={"flow_id": flow_id}
        )
        assert response.status_code == 200, response.text
        _assert_no_token_material(response.text)
        last = response.json()
        if last["state"] == "authorized":
            return last
        assert last["state"] == "pending", last
        time.sleep(0.05)
    pytest.fail(f"device flow did not authorize in {DEVICE_POLL_TIMEOUT_S}s: {last}")


def _likes_tracks(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(f"{API}/yandex/likes", params={"limit": 50, "offset": 0})
    assert response.status_code == 200, response.text
    _assert_no_token_material(response.text)
    payload = response.json()
    tracks = payload["items"]
    assert payload["total"] >= len(tracks)
    assert tracks, "mock likes page must not be empty"
    for track in tracks:
        assert track["source"] == "yandex"
        assert track["external_id"]
        assert track["availability"] == "online"
        assert track["cover_url"]
    return tracks


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_device_flow_authorizes_and_reports_status_without_tokens(
    p3_client: TestClient,
) -> None:
    start = _start_device_flow(p3_client)
    authorized = _wait_for_device_authorization(p3_client, start["flow_id"])
    assert authorized["flow_id"] == start["flow_id"]
    assert authorized["login"]

    status = p3_client.get(f"{API}/yandex/auth/status")
    assert status.status_code == 200, status.text
    _assert_no_token_material(status.text)
    payload = status.json()
    assert payload["authenticated"] is True
    assert payload["login"]
    assert payload["expires_at"]
    assert payload["source"] == "device"


# --------------------------------------------------------------------------- #
# Catalog -> player
# --------------------------------------------------------------------------- #


def test_likes_drive_play_seek_and_next(p3_client: TestClient) -> None:
    tracks = _likes_tracks(p3_client)
    assert len(tracks) >= 2, "needs at least two liked tracks for next"
    first, second = tracks[0], tracks[1]

    queue = p3_client.post(
        f"{API}/queue", json={"track_ids": [first["id"], second["id"]]}
    )
    assert queue.status_code == 201, queue.text

    played = p3_client.post(f"{API}/player/play", json={"track_id": first["id"]})
    assert played.status_code == 200, played.text
    _assert_no_token_material(played.text)
    state = played.json()
    assert state["status"] == "playing"
    assert state["track"]["id"] == first["id"]
    assert state["track"]["source"] == "yandex"

    seeked = p3_client.post(f"{API}/player/seek", json={"position_ms": 5000})
    assert seeked.status_code == 200, seeked.text
    assert seeked.json()["position_ms"] == 5000

    advanced = p3_client.get(f"{API}/player/state").json()
    assert advanced["position_ms"] >= 5000

    next_state = p3_client.post(f"{API}/player/next")
    assert next_state.status_code == 200, next_state.text
    next_payload = next_state.json()
    assert next_payload["track"]["id"] == second["id"]
    assert next_payload["track"]["source"] == "yandex"
    assert next_payload["queue_index"] == 1

    p3_client.post(f"{API}/player/stop")


# --------------------------------------------------------------------------- #
# Wave
# --------------------------------------------------------------------------- #


def test_wave_start_and_feedback_return_updated_state(p3_client: TestClient) -> None:
    started = p3_client.post(f"{API}/yandex/wave/start")
    assert started.status_code == 200, started.text
    _assert_no_token_material(started.text)
    wave = started.json()
    assert wave["station"] == "user:onyourwave"
    assert wave["batch_id"]
    assert wave["tracks"], "wave must return tracks"
    assert 0 <= wave["current_index"] < len(wave["tracks"])
    assert wave["updated_at"]
    for track in wave["tracks"]:
        assert track["source"] == "yandex"

    track_id = wave["tracks"][0]["id"]
    liked = p3_client.post(
        f"{API}/yandex/wave/feedback",
        json={"action": "like", "track_id": track_id, "played_seconds": 42},
    )
    assert liked.status_code == 200, liked.text
    _assert_no_token_material(liked.text)
    liked_wave = liked.json()
    assert liked_wave["station"] == "user:onyourwave"
    assert liked_wave["tracks"]
    assert all(track["source"] == "yandex" for track in liked_wave["tracks"])

    skipped = p3_client.post(
        f"{API}/yandex/wave/feedback",
        json={"action": "skip", "track_id": track_id, "played_seconds": 42},
    )
    assert skipped.status_code == 200, skipped.text
    skipped_wave = skipped.json()
    assert skipped_wave["tracks"], "wave refill must keep tracks available"
    assert all(track["source"] == "yandex" for track in skipped_wave["tracks"])


# --------------------------------------------------------------------------- #
# Stream (Range proxy)
# --------------------------------------------------------------------------- #


def test_stream_supports_repeated_range_requests(p3_client: TestClient) -> None:
    track = _likes_tracks(p3_client)[0]
    url = f"{API}/stream/{track['id']}"

    first = p3_client.get(url, headers={"Range": "bytes=0-1023"})
    assert first.status_code == 206, first.text
    assert first.headers["accept-ranges"] == "bytes"
    assert first.headers["content-type"].startswith("audio/")
    match = re.fullmatch(r"bytes 0-1023/(\d+)", first.headers["content-range"])
    assert match, first.headers["content-range"]
    total = int(match.group(1))
    assert total > 2047, "needs at least two full 1 KiB ranges"
    assert len(first.content) == 1024
    _assert_no_token_material(json.dumps(dict(first.headers)))

    second = p3_client.get(url, headers={"Range": "bytes=1024-2047"})
    assert second.status_code == 206, second.text
    assert second.headers["accept-ranges"] == "bytes"
    assert re.fullmatch(r"bytes 1024-2047/\d+", second.headers["content-range"])
    assert len(second.content) == 1024
    _assert_no_token_material(json.dumps(dict(second.headers)))


# --------------------------------------------------------------------------- #
# Two browser tabs over WebSocket
# --------------------------------------------------------------------------- #


def test_two_tabs_receive_snapshot_and_player_events(p3_client: TestClient) -> None:
    tracks = _likes_tracks(p3_client)
    assert len(tracks) >= 2
    first_track, second_track = tracks[0], tracks[1]
    p3_client.post(
        f"{API}/queue", json={"track_ids": [first_track["id"], second_track["id"]]}
    ).raise_for_status()

    with (
        p3_client.websocket_connect(f"{API}/ws") as tab_one,
        p3_client.websocket_connect(f"{API}/ws") as tab_two,
    ):
        for tab in (tab_one, tab_two):
            snapshot = tab.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["payload"]["server"]["mode"] == "mock"
            _assert_no_token_material(json.dumps(snapshot))
            tab.send_json({"type": "client.hello", "payload": {"visible": True}})

        p3_client.post(
            f"{API}/player/play", json={"track_id": first_track["id"]}
        ).raise_for_status()

        for tab in (tab_one, tab_two):
            event = _receive_until(
                tab,
                lambda message: message["type"] == "player.state_changed"
                and message["payload"]["state"]["status"] == "playing",
            )
            assert event["payload"]["state"]["track"]["id"] == first_track["id"]
            _assert_no_token_material(json.dumps(event))

        p3_client.post(f"{API}/player/next").raise_for_status()

        for tab in (tab_one, tab_two):
            event = _receive_until(
                tab,
                lambda message: message["type"] == "player.track_changed"
                and message["payload"]["state"]["track"]["id"] == second_track["id"],
            )
            assert event["payload"]["state"]["queue_index"] == 1

        p3_client.post(f"{API}/player/stop")

