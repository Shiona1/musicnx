"""Auto-advance must survive an unreachable online source.

The Pi can reach the Yandex catalog but not its CDN edge; the player may not
stop the whole queue on that failure. These tests replace the resolver seam with
a fake and drive EOF through the real PlayerManager.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from app.core.models import QueueItem, Track
from app.core.player_manager import PlayerManager
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def _track_by_title(client: TestClient, title: str) -> dict:
    items = client.get("/api/v1/library/tracks").json()["items"]
    return next(item for item in items if item["title"] == title)


def _finish_now(client: TestClient, track: dict) -> None:
    client.post(
        "/api/v1/player/seek",
        json={"position_ms": max(track["duration_ms"] - 100, 0)},
    )
    time.sleep(1.2)


def test_eof_skips_blocked_online_track(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        ctx = client.app.state.ctx
        short = _track_by_title(client, "Короткая проверка")
        blocked = _track_by_title(client, "Северное сияние")
        playable = _track_by_title(client, "Ночной эфир")
        client.post(
            "/api/v1/queue",
            json={"track_ids": [short["id"], blocked["id"], playable["id"]]},
        )
        monkeypatch.setattr(
            ctx.player, "_needs_cdn", lambda track: track.id == blocked["id"]
        )
        ctx.player._cdn_blocked_until = time.monotonic() + 300

        client.post("/api/v1/player/play", json={"track_id": short["id"]})
        _finish_now(client, short)

        state = client.get("/api/v1/player/state").json()
        assert state["track"]["id"] == playable["id"]
        assert state["status"] == "playing"


def test_eof_stops_when_only_online_tracks_remain(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        ctx = client.app.state.ctx
        short = _track_by_title(client, "Короткая проверка")
        blocked = _track_by_title(client, "Городские огни")
        client.post(
            "/api/v1/queue", json={"track_ids": [short["id"], blocked["id"]]}
        )
        monkeypatch.setattr(
            ctx.player, "_needs_cdn", lambda track: track.id == blocked["id"]
        )
        ctx.player._cdn_blocked_until = time.monotonic() + 300
        subscription = ctx.bus.subscribe()
        try:
            client.post("/api/v1/player/play", json={"track_id": short["id"]})
            _finish_now(client, short)
            events: list[dict] = []
            while True:
                try:
                    events.append(subscription.queue.get_nowait())
                except Exception:
                    break
        finally:
            ctx.bus.unsubscribe(subscription)

        state = client.get("/api/v1/player/state").json()
        assert state["status"] == "stopped"
        assert state["error_code"] is None
        assert any(
            event["type"] == "system.warning"
            and event["payload"].get("code") == "queue_exhausted_offline"
            for event in events
        )


def test_explicit_play_online_is_fast_and_does_not_skip(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        ctx = client.app.state.ctx
        blocked = _track_by_title(client, "Пыль дорог")
        monkeypatch.setattr(ctx.player, "_needs_cdn", lambda track: True)
        ctx.player._cdn_blocked_until = time.monotonic() + 300

        started = time.monotonic()
        response = client.post(
            "/api/v1/player/play", json={"track_id": blocked["id"]}
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "cdn_unreachable"
        assert elapsed < 2.0, "cached CDN failure must not probe again"
        state = client.get("/api/v1/player/state").json()
        assert state["track"]["id"] == blocked["id"]
        assert state["status"] == "error"


def test_expired_cdn_block_is_probed_again(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        ctx = client.app.state.ctx
        track = _track_by_title(client, "Тихий дождь")
        monkeypatch.setattr(ctx.player, "_needs_cdn", lambda item: True)
        ctx.player._cdn_blocked_until = time.monotonic() - 1

        response = client.post(
            "/api/v1/player/play", json={"track_id": track["id"]}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "playing"


def test_next_playable_index_skips_missing_and_blocked() -> None:
    player = object.__new__(PlayerManager)
    player._cdn_blocked_until = time.monotonic() + 300

    def item(track_id: int, **track_fields) -> QueueItem:
        track = Track(id=track_id, title=f"t{track_id}", **track_fields)
        return SimpleNamespace(track=track)  # type: ignore[return-value]

    items = [
        item(1, source="yandex", has_online_source=True),
        item(2, source="yandex", has_online_source=True, file_missing=True),
        item(3, source="yandex", has_online_source=True, has_local_file=True),
    ]
    assert player._next_playable_index(items, 0) == 2
    assert player._next_playable_index(items, 3) is None
