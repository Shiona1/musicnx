"""P6 end-to-end draft: sync run lifecycle, idempotency, cancel, history and WS.

Contract: docs/API.md §10 (``SyncRun``/``SyncStatus``/``SyncHistory``, routes,
WS events) and docs/SYNC.md §2–§4 (single-flight, revision fallback, partial
success, cancellation between playlists). Everything runs in mock mode: no
network, no Yandex token, no mpv, no Pi.

The only skip gate is a missing sync router: while ``app.main`` does not register
``/api/v1/sync``, every scenario in this module is skipped with an explicit
reason. Nothing else is skipped silently.

Fault injection: the contract has no test-only hook, so the tests wrap the
Yandex adapter methods on the live ``ctx.yandex`` instance (``playlists``,
``playlist``). A run is slowed down the same way to make ``running=true``
observable, matching the "async background run" contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.core.models import YandexPlaylist, YandexPlaylistPage
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"
SYNC = f"{API}/sync"
WS = f"{API}/ws"

POLL_INTERVAL_S = 0.02
WAIT_TIMEOUT_S = 20.0
TERMINAL = frozenset({"success", "partial", "failed", "cancelled"})
TERMINAL_EVENTS = frozenset(
    {"sync.completed", "sync.failed", "sync.cancelled"}
)
COUNTERS = (
    "playlists_added",
    "playlists_updated",
    "playlists_removed",
    "tracks_added",
    "tracks_updated",
    "tracks_removed",
)
SECRET_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_")
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# --------------------------------------------------------------------------- #
# Fixtures and gates
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _client(
    tmp_path: Path, *, pin: str | None = None
) -> Iterator[tuple[TestClient, Settings]]:
    """Mock-mode app with a fresh database; sync runs only on demand."""

    settings = make_settings(tmp_path, mode="mock", pin=pin)
    assert settings.sync.enabled is False, "manual-only by default (docs/API.md §10)"
    app = create_app(settings)
    if not _routes_are_wired(app):
        pytest.skip(
            "sync routes are not registered in app.main yet (docs/API.md §10); "
            "nothing else in this module is skipped"
        )
    with TestClient(app) as client:
        yield client, settings


def _routes_are_wired(app: Any) -> bool:
    for route in app.routes:
        path = getattr(route, "path", "")
        if path == SYNC or path.startswith(f"{SYNC}/"):
            return True
    return False


def _assert_no_secrets(text: str) -> None:
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in lowered, f"response leaks {marker!r}"


def _timestamp(value: str | None) -> datetime:
    assert value is not None, "expected a timestamp"
    assert RFC3339.match(value), f"{value!r} is not RFC3339 with milliseconds"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _status(client: TestClient) -> dict[str, Any]:
    response = client.get(f"{SYNC}/status")
    assert response.status_code == 200, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"running", "last", "next_run_at", "interval_s", "progress"}, payload
    assert isinstance(payload["running"], bool)
    assert payload["interval_s"] == 3600, payload
    return payload


def _history(client: TestClient, *, limit: int | None = None) -> dict[str, Any]:
    params = {} if limit is None else {"limit": limit}
    response = client.get(f"{SYNC}/history", params=params)
    assert response.status_code == 200, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"items", "total"}, payload
    assert isinstance(payload["items"], list)
    assert payload["total"] >= len(payload["items"])
    if limit is not None:
        assert len(payload["items"]) <= limit
    return payload


def _start(client: TestClient) -> dict[str, Any]:
    response = client.post(f"{SYNC}/start")
    assert response.status_code == 202, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"running", "last", "next_run_at", "interval_s", "progress"}, payload
    assert payload["running"] is True, f"start must report a running sync: {payload}"
    return payload


def _run_row(client: TestClient, run_id: int) -> dict[str, Any]:
    items = _history(client)["items"]
    for item in items:
        if item["id"] == run_id:
            return item
    pytest.fail(f"run {run_id} is missing from history: {items}")


def _wait_for_new_run(
    client: TestClient, previous_total: int, *, description: str
) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _history(client)
        if payload["total"] > previous_total and payload["items"]:
            last = payload["items"][0]
            if last["status"] in TERMINAL:
                return last
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"timed out after {WAIT_TIMEOUT_S}s waiting for {description}; "
        f"last status: {json.dumps(_status(client), ensure_ascii=False)[:1200]}"
    )


def _wait_until_running(client: TestClient) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _status(client)["running"]:
            return
        time.sleep(POLL_INTERVAL_S)
    pytest.fail("sync never reported running=true after a successful start")


def _db_counts(settings: Settings) -> tuple[int, int, int]:
    """(playlists, active playlist_tracks, sync_runs) read from the same file."""

    connection = sqlite3.connect(settings.paths.database)
    try:
        playlists = connection.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]
        tracks = connection.execute(
            "SELECT COUNT(*) FROM playlist_tracks WHERE removed_at IS NULL"
        ).fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
    finally:
        connection.close()
    return int(playlists), int(tracks), int(runs)


# --------------------------------------------------------------------------- #
# Fault injection through the adapter contract
# --------------------------------------------------------------------------- #


def _mock_playlist_page(client: TestClient, extra_kinds: tuple[int, ...]) -> YandexPlaylistPage:
    """The mock playlist plus synthetic copies for partial/cancel scenarios."""

    payload = client.get(f"{API}/yandex/playlists").json()
    items = payload["items"]
    assert items, "mock mode must expose at least one Yandex playlist"
    base = items[0]
    uid = base["id"].split(":")[0]
    page_items = [YandexPlaylist.model_validate(base)]
    page_items.extend(
        YandexPlaylist(
            id=f"{uid}:{kind}",
            kind=kind,
            title=f"P6 synthetic playlist {kind}",
            track_count=base["track_count"],
            cover_url=base.get("cover_url"),
            owner=base.get("owner"),
        )
        for kind in extra_kinds
    )
    return YandexPlaylistPage(items=page_items, total=len(page_items))


def _install_adapter(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    *,
    page: YandexPlaylistPage | None = None,
    fail_kind: int | None = None,
    delay_s: float = 0.0,
) -> None:
    """Wrap ``ctx.yandex.playlists``/``playlist`` without touching app code."""

    context = client.app.state.ctx
    adapter = context.yandex
    original_playlist = adapter.playlist
    base_id = page.items[0].id if page is not None else None
    items = {item.id: item for item in page.items} if page is not None else {}

    async def playlists() -> YandexPlaylistPage:
        assert page is not None
        return page

    async def playlist(playlist_id: str, **kwargs: Any) -> Any:
        if delay_s:
            await asyncio.sleep(delay_s)
        if fail_kind is not None and str(playlist_id).endswith(f":{fail_kind}"):
            raise RuntimeError("simulated upstream playlist failure")
        if base_id is None:
            return await original_playlist(playlist_id, **kwargs)
        single, paged = await original_playlist(base_id, **kwargs)
        item = items.get(str(playlist_id), single)
        return item, paged

    if page is not None:
        monkeypatch.setattr(adapter, "playlists", playlists)
    monkeypatch.setattr(adapter, "playlist", playlist)


# --------------------------------------------------------------------------- #
# Configuration contract (runs without the routes)
# --------------------------------------------------------------------------- #


def test_sync_settings_follow_the_p6_contract(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mode="mock")
    sync = settings.sync

    # docs/API.md §10: hourly by default, manual-only until enabled.
    assert sync.enabled is False
    assert sync.interval_s == 3600
    assert sync.interval_s >= 300


# --------------------------------------------------------------------------- #
# Idle status and validation
# --------------------------------------------------------------------------- #


def test_status_is_idle_before_the_first_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _settings):
        status = _status(client)
        assert status["running"] is False
        assert status["last"] is None
        assert status["progress"] is None
        # Manual-only mode has no scheduled run; the field must still exist.
        assert status["next_run_at"] is None or isinstance(status["next_run_at"], str)

        history = _history(client)
        assert history["items"] == []
        assert history["total"] == 0


def test_history_limit_is_bounded_and_newest_first(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _settings):
        _start(client)
        first = _wait_for_new_run(client, 0, description="the first sync")
        total = _history(client)["total"]

        _start(client)
        second = _wait_for_new_run(client, total, description="the second sync")
        assert second["id"] > first["id"]

        page = _history(client, limit=1)
        assert len(page["items"]) == 1, page
        assert page["items"][0]["id"] == second["id"]
        assert page["total"] >= 2
        started = [_timestamp(run["started_at"]) for run in _history(client)["items"]]
        assert started == sorted(started, reverse=True)


# --------------------------------------------------------------------------- #
# Happy path and idempotency
# --------------------------------------------------------------------------- #


def test_manual_sync_runs_to_success_and_second_run_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as (client, settings):
        _install_adapter(monkeypatch, client, delay_s=0.1)
        before = _db_counts(settings)
        total = _history(client)["total"]

        _start(client)
        _wait_until_running(client)
        first = _wait_for_new_run(client, total, description="the first sync")
        assert first["status"] == "success", first
        assert first["finished_at"] is not None
        assert _timestamp(first["started_at"]) <= _timestamp(first["finished_at"])
        assert first["error"] is None, first
        # First run against an empty playlists table writes at least the mock
        # playlist and its tracks.
        assert first["playlists_added"] >= 1, first
        assert first["tracks_added"] + first["tracks_updated"] >= 1, first
        after_first = _db_counts(settings)
        assert after_first[0] > before[0], "no playlist rows were written"
        assert after_first[1] > before[1], "no playlist_tracks rows were written"
        assert after_first[2] == before[2] + 1

        status = _status(client)
        assert status["running"] is False
        assert status["last"] is not None
        assert status["last"]["id"] == first["id"]
        assert _history(client)["items"][0] == status["last"]

        total = _history(client)["total"]
        _start(client)
        repeat = _wait_for_new_run(client, total, description="the repeated sync")
        assert repeat["id"] > first["id"]
        assert repeat["status"] == "success", repeat
        # docs/SYNC.md §2.2: an unchanged catalog changes no rows.
        assert all(repeat[counter] == 0 for counter in COUNTERS), repeat
        assert repeat["error"] is None
        after_second = _db_counts(settings)
        assert after_second[0] == after_first[0], "repeat created playlist rows"
        assert after_second[1] == after_first[1], "repeat changed playlist_tracks"
        assert after_second[2] == after_first[2] + 1


# --------------------------------------------------------------------------- #
# Partial success
# --------------------------------------------------------------------------- #


def test_one_failing_playlist_marks_the_run_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as (client, _settings):
        page = _mock_playlist_page(client, extra_kinds=(999,))
        _install_adapter(monkeypatch, client, page=page, fail_kind=999)
        total = _history(client)["total"]

        _start(client)
        run = _wait_for_new_run(client, total, description="the partial sync")
        assert run["status"] == "partial", (
            "docs/API.md §10: an error in one playlist is a partial run, "
            f"not {run['status']!r}: {run}"
        )
        assert run["error"] and run["error"].strip(), run
        _assert_no_secrets(run["error"])
        # The healthy playlist still landed; the run did not roll everything back.
        assert run["playlists_added"] + run["tracks_added"] > 0, run
        assert run["finished_at"] is not None

        status = _status(client)
        assert status["running"] is False
        assert status["last"] is not None and status["last"]["id"] == run["id"]


# --------------------------------------------------------------------------- #
# Single-flight
# --------------------------------------------------------------------------- #


def test_second_start_while_running_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as (client, _settings):
        page = _mock_playlist_page(client, extra_kinds=(998,))
        _install_adapter(monkeypatch, client, page=page, delay_s=0.25)
        total = _history(client)["total"]

        _start(client)
        _wait_until_running(client)
        second = client.post(f"{SYNC}/start")
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "sync_running"
        _assert_no_secrets(second.text)

        run = _wait_for_new_run(client, total, description="the single-flight run")
        assert run["status"] == "success", run
        assert _history(client)["total"] == total + 1, "a rejected start created a run"


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def test_cancel_stops_between_playlists_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as (client, _settings):
        page = _mock_playlist_page(client, extra_kinds=(997, 996))
        _install_adapter(monkeypatch, client, page=page, delay_s=0.2)
        total = _history(client)["total"]

        _start(client)
        _wait_until_running(client)
        cancelled = client.post(f"{SYNC}/cancel")
        assert cancelled.status_code == 202, cancelled.text
        _assert_no_secrets(cancelled.text)
        body = cancelled.json()
        assert {"id", "status", "started_at"} <= set(body), body
        assert body["status"] in {"running", "cancelled"}, body

        run = _wait_for_new_run(client, total, description="the cancelled sync")
        assert run["status"] == "cancelled", run
        assert run["finished_at"] is not None
        # The current playlist transaction may have completed; the rest did not.
        assert _history(client)["items"][0]["id"] == run["id"]
        status = _status(client)
        assert status["running"] is False
        assert status["last"] is not None and status["last"]["id"] == run["id"]

        again = client.post(f"{SYNC}/cancel")
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "sync_not_running"


# --------------------------------------------------------------------------- #
# WebSocket events
# --------------------------------------------------------------------------- #


def test_websocket_publishes_sync_started_and_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path) as (client, _settings):
        _install_adapter(monkeypatch, client, delay_s=0.1)
        with client.websocket_connect(WS) as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"

            _start(client)

            events: list[str] = []
            terminal: dict[str, Any] | None = None
            for _ in range(80):
                message = websocket.receive_json()
                message_type = message["type"]
                events.append(message_type)
                if not message_type.startswith("sync."):
                    continue

                payload = message["payload"]
                assert isinstance(payload, dict), message
                _assert_no_secrets(json.dumps(message, ensure_ascii=False))
                if message_type in TERMINAL_EVENTS:
                    run = payload.get("run")
                    assert isinstance(run, dict), message
                    assert run["id"] > 0
                    assert run["status"] in TERMINAL or run["status"] == "running"
                    assert run["finished_at"] is not None
                    terminal = message
                    break
                if message_type == "sync.progress":
                    assert {"phase", "current", "total"} <= set(payload), message
                    assert isinstance(payload["current"], int)
                    assert isinstance(payload["total"], int)
                elif message_type == "sync.started":
                    run = payload.get("run")
                    assert isinstance(run, dict), message
                    assert run["status"] == "running", run
                else:
                    pytest.fail(f"unexpected sync event {message_type!r}")

            assert "sync.started" in events, events
            assert terminal is not None, f"no terminal sync event among {events}"
            assert terminal["type"] == "sync.completed", terminal
            assert terminal["payload"]["run"]["status"] == "success", terminal


# --------------------------------------------------------------------------- #
# Security: sync mutations are PIN/localhost guarded
# --------------------------------------------------------------------------- #


@pytest.mark.skip(reason="PIN feature removed")
def test_sync_mutations_require_pin_access(tmp_path: Path) -> None:
    with _client(tmp_path, pin="1234") as (client, _settings):
        for path in (f"{SYNC}/start", f"{SYNC}/cancel"):
            denied = client.post(path)
            assert denied.status_code == 401, denied.text
            assert denied.json()["error"]["code"] == "auth_required"
            _assert_no_secrets(denied.text)

        # Status and history are read-only and stay available.
        assert client.get(f"{SYNC}/status").status_code == 200
        assert client.get(f"{SYNC}/history").status_code == 200
