"""P6 sync engine and API contract tests (docs/API.md §10).

Everything runs against a real migrated SQLite database in a temporary
directory and fake/mock Yandex adapters: no network, no token, no Raspberry Pi
(AGENTS.md §4). The fake adapter returns internal ``Track`` DTOs, exactly like
the frozen adapter contract guarantees.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes.sync import router as sync_router
from app.auth import COOKIE_NAME, issue_session
from app.config import Settings, SyncSettings
from app.core.event_bus import EventBus
from app.core.models import (
    LibraryPage,
    Track,
    YandexPlaylist,
    YandexPlaylistPage,
)
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.repositories import TrackRepository
from app.database.seed import seed_mock_catalog
from app.errors import ConflictError, UnavailableError
from app.integrations.yandex_music.mock_adapter import MockYandexAdapter
from app.integrations.yandex_music.repository import YandexRepository
from app.sync.repository import SyncRepository
from app.sync.service import SyncService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import make_settings

# --------------------------------------------------------------------- helpers


def _sync_settings(
    tmp_path: Path, *, enabled: bool = False, interval_s: float = 3600
) -> Settings:
    """Test settings; ``model_copy`` lets the scheduler test use a tiny interval."""

    settings = make_settings(tmp_path)
    sync = SyncSettings(enabled=enabled).model_copy(
        update={"interval_s": interval_s}
    )
    return settings.model_copy(update={"sync": sync})


def _service(
    settings: Settings,
    db: Database,
    adapter: Any,
    *,
    max_attempts: int = 3,
    backoff_s: tuple[float, ...] = (0.0,),
) -> tuple[SyncService, EventBus]:
    bus = EventBus()
    tracks = TrackRepository(db)
    service = SyncService(
        settings,
        bus,
        db,
        adapter,
        YandexRepository(db, tracks),
        tracks,
        max_attempts=max_attempts,
        backoff_s=backoff_s,
    )
    return service, bus


async def _tracks(db: Database, count: int = 4) -> list[Track]:
    items, total = await TrackRepository(db).list(limit=count)
    assert total >= count
    return items


async def _drain(subscription: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(subscription.queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


class FakeAdapter:
    """Two-playlist adapter honouring the frozen contract (no repository IO)."""

    mode = "mock"

    def __init__(
        self,
        playlists: dict[str, list[Track]],
        *,
        errors: dict[str, Exception] | None = None,
        playlists_error_count: int = 0,
        playlists_error: Exception | None = None,
    ) -> None:
        self._playlists = playlists
        self._errors = errors or {}
        self._playlists_error_count = playlists_error_count
        self._playlists_error = playlists_error
        self.playlists_calls = 0
        self.playlist_calls: list[str] = []
        self.gate: tuple[threading.Event, threading.Event] | None = None

    def _dto(self, playlist_id: str) -> YandexPlaylist:
        uid, _, kind = playlist_id.partition(":")
        return YandexPlaylist(
            id=playlist_id,
            kind=int(kind) if kind.isdigit() else 0,
            title=f"Playlist {kind}",
            track_count=len(self._playlists[playlist_id]),
            owner=uid,
        )

    async def playlists(self) -> YandexPlaylistPage:
        self.playlists_calls += 1
        if (
            self._playlists_error is not None
            and self.playlists_calls <= self._playlists_error_count
        ):
            raise self._playlists_error
        items = [self._dto(playlist_id) for playlist_id in self._playlists]
        return YandexPlaylistPage(items=items, total=len(items))

    async def playlist(
        self, playlist_id: str, *, limit: int, offset: int
    ) -> tuple[YandexPlaylist, LibraryPage]:
        self.playlist_calls.append(playlist_id)
        if self.gate is not None:
            started, release = self.gate
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        if playlist_id in self._errors:
            raise self._errors[playlist_id]
        tracks = self._playlists[playlist_id]
        return (
            self._dto(playlist_id),
            LibraryPage(
                items=tracks[offset : offset + limit],
                total=len(tracks),
                limit=limit,
                offset=offset,
            ),
        )

    async def close(self) -> None:  # pragma: no cover - parity with the protocol
        return None


class RateLimitedError(Exception):
    """Minimal upstream 429 shape (the real adapter maps it to 503)."""

    status_code = 429


class GatedMockAdapter:
    """Mock adapter wrapper pausing every playlist fetch until released."""

    mode = "mock"

    def __init__(
        self,
        adapter: MockYandexAdapter,
        started: threading.Event,
        release: threading.Event,
    ) -> None:
        self._adapter = adapter
        self._started = started
        self._release = release

    async def playlists(self) -> YandexPlaylistPage:
        return await self._adapter.playlists()

    async def playlist(
        self, playlist_id: str, *, limit: int, offset: int
    ) -> tuple[YandexPlaylist, LibraryPage]:
        self._started.set()
        while not self._release.is_set():
            await asyncio.sleep(0.005)
        return await self._adapter.playlist(playlist_id, limit=limit, offset=offset)

    async def close(self) -> None:
        await self._adapter.close()


async def _run_once(service: SyncService) -> SyncService:
    await service.start(reason="manual")
    await service.wait_idle()
    return service


# ------------------------------------------------------------------ repository


async def test_sync_repository_run_lifecycle(db: Database) -> None:
    repository = SyncRepository(db)
    assert await repository.latest() is None
    assert await repository.count() == 0

    first = await repository.start_run()
    assert first.id > 0
    assert first.status == "running"
    assert first.finished_at is None
    assert first.started_at.endswith("Z")
    assert first.tracks_added == 0

    finished = await repository.finish_run(
        first.id, "success", {"tracks_added": 3, "playlists_added": 1}, None
    )
    assert finished is not None
    assert finished.status == "success"
    assert finished.finished_at is not None
    assert finished.tracks_added == 3
    assert finished.playlists_added == 1
    assert (await repository.get(first.id)) == finished

    second = await repository.start_run()
    history = await repository.history(10)
    assert [run.id for run in history] == [second.id, first.id]
    assert await repository.count() == 2
    assert (await repository.latest()).id == second.id
    # status.last reports the last completed attempt, not the active row.
    latest_finished = await repository.latest_finished()
    assert latest_finished is not None and latest_finished.id == first.id
    assert await repository.get(999_999) is None

    # A crashed process leaves rows "running"; startup must close them.
    assert await repository.close_stale_running("interrupted by restart") == 1
    stale = await repository.get(second.id)
    assert stale is not None and stale.status == "failed"
    assert stale.error == "interrupted by restart"


# ---------------------------------------------------------------- run pipeline


async def test_sync_composition_and_idempotent_rerun(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 4)
    adapter = FakeAdapter({"1001:7": tracks[:2], "1001:8": tracks[2:3]})
    service, bus = _service(_sync_settings(tmp_path), db, adapter)
    subscription = bus.subscribe()

    await service.start(reason="manual")
    await service.wait_idle()
    status = await service.status()

    assert status.running is False
    assert status.last is not None
    first = status.last
    assert first.status == "success"
    assert first.playlists_added == 2
    assert first.playlists_updated == 0
    assert first.tracks_added == 3
    assert first.tracks_updated == 0
    assert first.tracks_removed == 0
    assert first.error is None

    repository = SyncRepository(db)
    playlist_id = await repository.playlist_ref("1001:7")
    assert playlist_id is not None
    yandex_repository = YandexRepository(db, TrackRepository(db))
    assert await yandex_repository.playlist_track_ids(playlist_id) == [
        tracks[0].id,
        tracks[1].id,
    ]

    events = await _drain(subscription)
    event_types = [event["type"] for event in events]
    assert event_types[0] == "sync.started"
    assert event_types[-1] == "sync.completed"
    assert event_types.count("sync.progress") >= 2
    assert events[-1]["payload"]["run"]["status"] == "success"

    # Rerun with unchanged remote data: nothing is added, moved or removed.
    await service.start(reason="manual")
    await service.wait_idle()
    second = (await service.status()).last
    assert second is not None and second.id != first.id
    assert second.status == "success"
    assert second.playlists_added == 0
    assert second.playlists_updated == 0
    assert second.tracks_added == 0
    assert second.tracks_updated == 0
    assert second.tracks_removed == 0
    assert await repository.count() == 2


async def test_sync_mirrors_removed_and_reordered_tracks(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 4)
    remote = {"1001:7": tracks[:3]}
    adapter = FakeAdapter(remote)
    service, _ = _service(_sync_settings(tmp_path), db, adapter)
    await _run_once(service)

    # Remote lost tracks[1] and moved tracks[2] to the front.
    remote["1001:7"] = [tracks[2], tracks[0]]
    await _run_once(service)
    last = (await service.status()).last
    assert last is not None
    assert last.tracks_added == 0
    assert last.tracks_removed == 1
    assert last.tracks_updated == 2
    assert last.playlists_updated == 1

    repository = SyncRepository(db)
    playlist_id = await repository.playlist_ref("1001:7")
    assert playlist_id is not None
    yandex_repository = YandexRepository(db, TrackRepository(db))
    assert await yandex_repository.playlist_track_ids(playlist_id) == [
        tracks[2].id,
        tracks[0].id,
    ]


async def test_one_broken_playlist_yields_partial_and_redacted_error(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 4)
    secret = "https://upstream.example/file?access_token=SUPERSECRET"
    adapter = FakeAdapter(
        {"1001:7": tracks[:2], "1001:8": tracks[2:3]},
        errors={"1001:8": RuntimeError(f"upstream exploded {secret}")},
    )
    service, bus = _service(_sync_settings(tmp_path), db, adapter)
    subscription = bus.subscribe()

    await _run_once(service)
    last = (await service.status()).last
    assert last is not None
    assert last.status == "partial"
    assert last.tracks_added == 2
    assert last.playlists_added == 1
    assert last.error is not None
    assert "1001:8" in last.error
    assert "[url]" in last.error
    assert "SUPERSECRET" not in last.error
    assert "access_token" not in last.error

    # A broken playlist is not retried and its row is never created.
    assert adapter.playlist_calls.count("1001:8") == 1
    assert await SyncRepository(db).playlist_ref("1001:8") is None

    events = await _drain(subscription)
    assert events[-1]["type"] == "sync.completed"
    assert events[-1]["payload"]["run"]["status"] == "partial"


async def test_all_playlists_broken_marks_run_failed(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 2)
    adapter = FakeAdapter(
        {"1001:7": tracks[:1], "1001:8": tracks[1:]},
        errors={
            "1001:7": RuntimeError("boom-a"),
            "1001:8": RuntimeError("boom-b"),
        },
    )
    service, _ = _service(_sync_settings(tmp_path), db, adapter)
    await _run_once(service)
    last = (await service.status()).last
    assert last is not None
    assert last.status == "failed"
    assert last.tracks_added == 0
    assert last.error is not None
    assert "boom-a" in last.error and "boom-b" in last.error


async def test_cancel_stops_between_playlists(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 4)
    adapter = FakeAdapter({"1001:7": tracks[:2], "1001:8": tracks[2:3]})
    started = threading.Event()
    release = threading.Event()
    adapter.gate = (started, release)
    service, bus = _service(_sync_settings(tmp_path), db, adapter)
    subscription = bus.subscribe()

    await service.start(reason="manual")
    start_snapshot = await service.status()
    assert start_snapshot.running is True
    assert start_snapshot.last is None  # no completed run yet
    assert await asyncio.to_thread(started.wait, 2.0)
    running = await service.cancel()
    assert running.status == "running"

    release.set()
    await service.wait_idle()
    status = await service.status()
    assert status.running is False
    assert status.last is not None
    assert status.last.status == "cancelled"
    # The first playlist finished; the second was never requested.
    assert status.last.tracks_added == 2
    assert status.last.playlists_added == 1
    assert adapter.playlist_calls == ["1001:7"]

    events = await _drain(subscription)
    assert events[-1]["type"] == "sync.cancelled"

    with pytest.raises(ConflictError) as error:
        await service.cancel()
    assert error.value.code == "sync_not_running"
    assert error.value.status_code == 409


async def test_start_is_single_flight(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 2)
    adapter = FakeAdapter({"1001:7": tracks[:1]})
    started = threading.Event()
    release = threading.Event()
    adapter.gate = (started, release)
    service, _ = _service(_sync_settings(tmp_path), db, adapter)

    await service.start(reason="manual")
    assert await asyncio.to_thread(started.wait, 2.0)
    with pytest.raises(ConflictError) as error:
        await service.start(reason="manual")
    assert error.value.code == "sync_running"
    assert error.value.status_code == 409

    release.set()
    await service.wait_idle()
    status = await service.status()
    assert status.last is not None and status.last.status == "success"
    first = status.last
    # Only one run row exists: the rejected start never touched the database.
    assert await SyncRepository(db).count() == 1

    # While a new run is active, ``status.last`` keeps the previous completed
    # attempt (docs/SYNC.md §3); the active run is visible through ``running``.
    started.clear()
    release.clear()
    adapter.gate = (started, release)
    await service.start(reason="manual")
    assert await asyncio.to_thread(started.wait, 2.0)
    mid = await service.status()
    assert mid.running is True
    assert mid.last is not None and mid.last.id == first.id
    release.set()
    await service.wait_idle()
    assert await SyncRepository(db).count() == 2


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnavailableError("temporarily unavailable"), id="5xx-unavailable"),
        pytest.param(RateLimitedError("rate limited"), id="429-rate-limited"),
    ],
)
async def test_transient_adapter_errors_are_retried_with_backoff(
    db: Database, tmp_path: Path, error: Exception
) -> None:
    tracks = await _tracks(db, 2)
    adapter = FakeAdapter(
        {"1001:7": tracks},
        playlists_error_count=2,
        playlists_error=error,
    )
    service, _ = _service(
        _sync_settings(tmp_path), db, adapter, max_attempts=3, backoff_s=(0.0,)
    )
    await _run_once(service)
    last = (await service.status()).last
    assert last is not None
    assert last.status == "success"
    assert last.tracks_added == 2
    assert adapter.playlists_calls == 3


# ------------------------------------------------------------ status/history


async def test_status_and_history_are_empty_until_a_run(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 1)
    service, _ = _service(
        _sync_settings(tmp_path), db, FakeAdapter({"1001:7": tracks})
    )
    status = await service.status()
    assert status.running is False
    assert status.last is None
    assert status.next_run_at is None
    assert status.interval_s == 3600
    assert status.progress is None

    history = await service.history()
    assert history.items == []
    assert history.total == 0

    # Disabled scheduler: startup must not create a timer or a run.
    await service.startup()
    await asyncio.sleep(0.05)
    assert (await service.status()).next_run_at is None
    assert (await service.history()).total == 0

    await _run_once(service)
    one = await service.history(limit=1)
    assert one.total == 1
    assert len(one.items) == 1
    assert one.items[0].status == "success"
    # limit below 1 or above 100 is bounded by the service/API contract.
    assert len((await service.history(limit=0)).items) == 1
    assert len((await service.history(limit=1000)).items) == 1


async def test_scheduler_runs_only_when_enabled(
    db: Database, tmp_path: Path
) -> None:
    tracks = await _tracks(db, 1)
    adapter = FakeAdapter({"1001:7": tracks})
    service, _ = _service(
        _sync_settings(tmp_path, enabled=True, interval_s=0.1), db, adapter
    )
    try:
        await service.startup()
        status = await service.status()
        assert status.next_run_at is not None

        deadline = asyncio.get_running_loop().time() + 3.0
        history = await service.history()
        # The row is inserted as ``running`` before the run body finishes; wait
        # for a terminal status instead of racing the first insert.
        while not history.items or history.items[0].status == "running":
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
            history = await service.history()
        assert history.items[0].status == "success"
    finally:
        await service.stop()

    stopped = await service.status()
    assert stopped.running is False
    assert stopped.next_run_at is None


async def test_mock_adapter_is_synced_into_playlist_tables(
    db: Database, tmp_path: Path
) -> None:
    yandex_repository = YandexRepository(db, TrackRepository(db))
    adapter = MockYandexAdapter(yandex_repository)
    service, _ = _service(_sync_settings(tmp_path), db, adapter)

    await _run_once(service)
    last = (await service.status()).last
    assert last is not None
    assert last.status == "success"
    assert last.playlists_added == 1
    assert last.tracks_added == 4

    repository = SyncRepository(db)
    playlist_id = await repository.playlist_ref("1001:7")
    assert playlist_id is not None
    assert len(await yandex_repository.playlist_track_ids(playlist_id)) == 4

    await _run_once(service)
    rerun = (await service.status()).last
    assert rerun is not None
    assert rerun.tracks_added == 0
    assert rerun.tracks_removed == 0
    assert rerun.tracks_updated == 0


# ---------------------------------------------------------------------- API


AdapterFactory = Callable[[Database], Awaitable[Any]]


def _mock_factory(
    gate: tuple[threading.Event, threading.Event] | None = None,
) -> AdapterFactory:
    async def factory(db: Database) -> Any:
        repository = YandexRepository(db, TrackRepository(db))
        adapter = MockYandexAdapter(repository)
        return (
            GatedMockAdapter(adapter, gate[0], gate[1]) if gate is not None else adapter
        )

    return factory


def _build_app(
    settings: Settings,
    adapter_factory: AdapterFactory,
    *,
    context_attr: str = "sync",
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.paths.database)
        await database.connect()
        await apply_migrations(database)
        await seed_mock_catalog(database)
        bus = EventBus()
        tracks = TrackRepository(database)
        adapter = await adapter_factory(database)
        service = SyncService(
            settings,
            bus,
            database,
            adapter,
            YandexRepository(database, tracks),
            tracks,
            backoff_s=(0.0,),
        )
        await service.startup()
        app.state.ctx = SimpleNamespace(
            settings=settings,
            bus=bus,
            **{context_attr: service},
        )
        try:
            yield
        finally:
            await service.stop()
            await database.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(sync_router, prefix="/api/v1")
    # Reuse the production error envelope (AppError/422/404 handlers).
    from app.main import _register_error_handlers

    _register_error_handlers(app)
    return app


def _wait_until_idle(client: TestClient, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get("/api/v1/sync/status").json()
        if not payload["running"]:
            return payload
        time.sleep(0.01)
    raise AssertionError("sync run did not finish in time")


def test_sync_status_and_history_endpoints_are_open(tmp_path: Path) -> None:
    with TestClient(_build_app(_sync_settings(tmp_path), _mock_factory())) as client:
        status = client.get("/api/v1/sync/status")
        assert status.status_code == 200
        payload = status.json()
        assert payload["running"] is False
        assert payload["last"] is None
        assert payload["interval_s"] == 3600
        assert payload["next_run_at"] is None
        assert payload["progress"] is None

        history = client.get("/api/v1/sync/history")
        assert history.status_code == 200
        assert history.json() == {"items": [], "total": 0}

        assert client.get("/api/v1/sync/history", params={"limit": 0}).status_code == 422
        assert (
            client.get("/api/v1/sync/history", params={"limit": 101}).status_code == 422
        )


def test_sync_start_returns_202_and_finishes_in_background(tmp_path: Path) -> None:
    with TestClient(_build_app(_sync_settings(tmp_path), _mock_factory())) as client:
        response = client.post("/api/v1/sync/start")
        assert response.status_code == 202
        assert response.json()["running"] is True

        payload = _wait_until_idle(client)
        last = payload["last"]
        assert last is not None
        assert last["status"] == "success"
        assert last["playlists_added"] == 1
        assert last["tracks_added"] == 4

        history = client.get("/api/v1/sync/history").json()
        assert history["total"] == 1
        assert history["items"][0]["id"] == last["id"]


def test_sync_start_conflicts_while_running(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    app = _build_app(_sync_settings(tmp_path), _mock_factory((started, release)))
    with TestClient(app) as client:
        assert client.post("/api/v1/sync/start").status_code == 202
        assert started.wait(5.0)
        conflict = client.post("/api/v1/sync/start")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "sync_running"

        release.set()
        payload = _wait_until_idle(client)
        assert payload["last"]["status"] == "success"


def test_sync_cancel_returns_the_active_run(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    app = _build_app(_sync_settings(tmp_path), _mock_factory((started, release)))
    with TestClient(app) as client:
        assert client.post("/api/v1/sync/start").status_code == 202
        assert started.wait(5.0)
        response = client.post("/api/v1/sync/cancel")
        assert response.status_code == 202
        run = response.json()
        assert run["status"] == "running"

        release.set()
        payload = _wait_until_idle(client)
        assert payload["last"]["id"] == run["id"]
        assert payload["last"]["status"] == "cancelled"

        idle = client.post("/api/v1/sync/cancel")
        assert idle.status_code == 409
        assert idle.json()["error"]["code"] == "sync_not_running"


@pytest.mark.skip(reason="PIN feature removed")
def test_sync_mutations_follow_the_shared_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.api.deps as deps

    app = _build_app(_sync_settings(tmp_path), _mock_factory())
    with TestClient(app) as client:
        monkeypatch.setattr(deps, "client_is_loopback", lambda request: False)
        forbidden = client.post("/api/v1/sync/start")
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "pin_required"
        # Reads stay open for LAN clients.
        assert client.get("/api/v1/sync/status").status_code == 200


@pytest.mark.skip(reason="PIN feature removed")
def test_sync_mutations_require_pin_when_configured(tmp_path: Path) -> None:
    settings = _sync_settings(tmp_path).model_copy(
        update={"security": make_settings(tmp_path, pin="1234").security}
    )
    with TestClient(_build_app(settings, _mock_factory())) as client:
        assert client.get("/api/v1/sync/status").status_code == 200
        unauthorized = client.post("/api/v1/sync/start")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "auth_required"

        # A valid PIN session (same cookie the login route sets) unlocks mutations.
        client.cookies.set(COOKIE_NAME, issue_session(settings.security))
        assert client.post("/api/v1/sync/start").status_code == 202
        _wait_until_idle(client)


def test_sync_router_accepts_the_sync_service_fallback(tmp_path: Path) -> None:
    app = _build_app(
        _sync_settings(tmp_path), _mock_factory(), context_attr="sync_service"
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/sync/status").status_code == 200


def test_sync_without_a_configured_service_is_unavailable(tmp_path: Path) -> None:
    settings = _sync_settings(tmp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ctx = SimpleNamespace(settings=settings)
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(sync_router, prefix="/api/v1")

    from app.main import _register_error_handlers

    _register_error_handlers(app)

    with TestClient(app) as client:
        response = client.get("/api/v1/sync/status")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unavailable"
