"""P7 audio zones: repository, manager, PipeWire router and API contract.

Everything runs against a temporary migrated SQLite database with fake routers
and players: no PipeWire, no mpv, no Raspberry Pi, no network (AGENTS.md §4).
The HTTP tests mount the real router on a minimal app and inject the services on
``app.state.ctx``, mirroring the lyrics/sync tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes.zones import router as zones_router
from app.auth import COOKIE_NAME, issue_session
from app.config import Settings
from app.core.event_bus import EventBus, Subscription
from app.core.models import BluetoothDevice
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.seed import seed_mock_catalog
from app.errors import AppError, NotFoundError, UnavailableError
from app.zones.manager import ZoneManager
from app.zones.repository import (
    ZONE_OFFSET_MAX_MS,
    ZONE_OFFSET_MIN_MS,
    ZoneRepository,
)
from app.zones.router import (
    CommandResult,
    PipeWireRouter,
    SinkInfo,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import make_settings

# --------------------------------------------------------------------------- fakes


class FakeZoneRouter:
    """Records routing calls and never touches the host audio stack."""

    backend = "fake"

    def __init__(self, sinks: Sequence[SinkInfo] | None = None) -> None:
        self.sinks = list(sinks or [SinkInfo(name="fake_sink", description="Fake")])
        self.calls: list[tuple[str, ...]] = []
        self.activated: list[str] = []
        self.volumes: list[tuple[str, int]] = []
        self.fail_activate = False

    async def list_sinks(self) -> list[SinkInfo]:
        return list(self.sinks)

    async def activate(self, sink_name: str) -> bool:
        self.calls.append(("activate", sink_name))
        if self.fail_activate or sink_name not in {sink.name for sink in self.sinks}:
            return False
        self.activated.append(sink_name)
        return True

    async def set_volume(self, sink_name: str, volume: int) -> bool:
        self.calls.append(("volume", sink_name, str(volume)))
        self.volumes.append((sink_name, volume))
        return True

    async def is_available(self, sink_name: str) -> bool:
        return any(sink.name == sink_name for sink in self.sinks)

    async def close(self) -> None:
        self.calls.append(("close",))


class FakePlayer:
    """Minimal PlayerManager surface: volume changes but playback never stops."""

    def __init__(self) -> None:
        self.volume: int | None = None
        self.calls: list[tuple[str, int]] = []
        self.stop_calls = 0

    async def set_volume(self, volume: int) -> None:
        self.volume = volume
        self.calls.append(("set_volume", volume))

    async def stop(self) -> None:  # pragma: no cover - must never be called
        self.stop_calls += 1


class RecordingRunner:
    """Async command runner returning canned results and recording argv."""

    def __init__(self, results: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.default = CommandResult(returncode=0)

    async def __call__(
        self, args: Sequence[str], timeout_s: float
    ) -> CommandResult:
        self.calls.append((tuple(args), timeout_s))
        return self.results.get(tuple(args), self.default)


@pytest.fixture
async def zone_manager(
    settings: Settings, db: Database
) -> AsyncIterator[tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus]]:
    router = FakeZoneRouter()
    player = FakePlayer()
    bus = EventBus()
    manager = ZoneManager(settings, bus, db, router, player)
    await manager.ensure_ready()
    yield manager, router, player, bus


def _subscription(bus: EventBus) -> Subscription:
    return bus.subscribe()


async def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


# ---------------------------------------------------------------------- repository


async def test_ensure_defaults_is_idempotent(settings: Settings, db: Database) -> None:
    repo = ZoneRepository(db)
    await repo.ensure_defaults()
    await repo.ensure_defaults()

    zones = await repo.list()
    assert [zone.id for zone in zones] == ["local", "browser", "bt"]
    assert [zone.kind for zone in zones] == ["local_sink", "browser", "bt"]
    assert zones[0].name == "Local output"
    assert zones[0].active is True
    assert zones[1].active is False
    # The BT row is a placeholder until an A2DP sink is attached.
    assert zones[2].available is False
    rows = await db.fetchall("SELECT id FROM audio_zones ORDER BY id")
    assert len(rows) == 3


async def test_zone_state_persists_across_repositories(
    settings: Settings, db: Database
) -> None:
    repo = ZoneRepository(db)
    await repo.ensure_defaults()
    assert await repo.mark_active("browser") is not None
    local = await repo.set_volume("local", 42)
    assert local is not None and local.volume == 42
    offset = await repo.set_offset("bt", 220)
    assert offset is not None and offset.offset_ms == 220
    assert await repo.set_sink_name("bt", "bluez_output.AA_BB.1") is not None

    reloaded = ZoneRepository(db)
    local = await reloaded.get("local")
    bt = await reloaded.get("bt")
    assert local is not None and local.volume == 42
    assert bt is not None and bt.offset_ms == 220
    assert bt.sink_name == "bluez_output.AA_BB.1"
    assert bt.available is True
    assert await reloaded.active_zone_id() == "browser"
    zones = await reloaded.list()
    assert {zone.id: zone.active for zone in zones}["browser"] is True
    assert {zone.id: zone.active for zone in zones}["local"] is False


async def test_touch_and_mark_active_update_last_seen(
    settings: Settings, db: Database
) -> None:
    repo = ZoneRepository(db)
    await repo.ensure_defaults()
    assert await repo.touch("bt") is True
    row = await db.fetchone("SELECT last_seen FROM audio_zones WHERE id = 'bt'")
    assert row is not None and row["last_seen"]
    active = await repo.mark_active("bt")
    assert active is not None and active.active is True
    assert await repo.touch("missing") is False


async def test_repository_rejects_out_of_bounds_values(
    settings: Settings, db: Database
) -> None:
    repo = ZoneRepository(db)
    await repo.ensure_defaults()
    with pytest.raises(ValueError):
        await repo.set_volume("local", -1)
    with pytest.raises(ValueError):
        await repo.set_volume("local", 101)
    with pytest.raises(ValueError):
        await repo.set_offset("local", ZONE_OFFSET_MAX_MS + 1)
    with pytest.raises(ValueError):
        await repo.set_offset("local", ZONE_OFFSET_MIN_MS - 1)
    assert await repo.set_volume("missing", 10) is None
    assert await repo.get("missing") is None


# ------------------------------------------------------------------------- manager


async def test_activate_local_switches_sink_without_stopping_playback(
    settings: Settings,
    db: Database,
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, router, player, bus = zone_manager
    subscription = _subscription(bus)

    zone = await manager.activate("local")

    assert zone.active is True
    assert zone.sink_name == "fake_sink"
    assert router.activated == ["fake_sink"]
    assert player.volume == zone.volume
    assert player.stop_calls == 0
    events = await _drain(subscription.queue)
    bus.unsubscribe(subscription)
    assert [event["type"] for event in events] == ["zones.updated"]
    assert events[0]["payload"]["zones"]["total"] == 3
    # Persisted across a fresh repository instance.
    persisted = ZoneRepository(db)
    assert (await persisted.active_zone_id()) == "local"


async def test_activate_bt_uses_a2dp_sink_and_keeps_playing(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, router, player, _bus = zone_manager
    device = BluetoothDevice(
        address="AA:BB:CC:DD:EE:01", name="Mock Speaker", paired=True, connected=True
    )
    await manager.attach_bluetooth(device)
    # The real router discovers the A2DP sink once BlueZ connects the device.
    router.sinks.append(
        SinkInfo(name="bluez_output.AA_BB_CC_DD_EE_01.1", description="A2DP")
    )

    zone = await manager.activate("bt")

    assert zone.sink_name == "bluez_output.AA_BB_CC_DD_EE_01.1"
    assert router.activated == ["bluez_output.AA_BB_CC_DD_EE_01.1"]
    assert player.stop_calls == 0
    assert zone.active is True


async def test_browser_zone_is_virtual_and_never_touches_routing(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, router, player, _bus = zone_manager

    zone = await manager.activate("browser")

    assert zone.active is True
    assert router.activated == []
    assert player.stop_calls == 0


async def test_volume_updates_db_router_and_active_player_only(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, router, player, _bus = zone_manager
    await manager.activate("local")

    updated = await manager.set_volume("local", 37)

    assert updated.volume == 37
    assert router.volumes[-1] == ("fake_sink", 37)
    assert player.volume == 37
    # Inactive zones persist their volume without touching the master player.
    await manager.set_volume("browser", 12)
    assert player.volume == 37
    assert (await manager.get("browser")).volume == 12


async def test_offset_is_stored_but_not_applied_to_playback(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, _router, player, _bus = zone_manager
    await manager.activate("local")
    calls_before = list(player.calls)

    updated = await manager.set_offset("bt", 250)

    assert updated.offset_ms == 250
    assert (await manager.get("bt")).offset_ms == 250
    assert player.calls == calls_before


async def test_unknown_zone_is_not_found(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, _router, _player, _bus = zone_manager
    with pytest.raises(NotFoundError):
        await manager.activate("nope")
    with pytest.raises(NotFoundError):
        await manager.set_volume("nope", 10)
    with pytest.raises(NotFoundError):
        await manager.set_offset("nope", 10)


async def test_manager_volume_bounds_raise_validation_error(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, _router, _player, _bus = zone_manager
    for volume in (-1, 101):
        with pytest.raises(AppError) as excinfo:
            await manager.set_volume("local", volume)
        assert excinfo.value.status_code == 422
    for offset in (ZONE_OFFSET_MIN_MS - 1, ZONE_OFFSET_MAX_MS + 1):
        with pytest.raises(AppError) as excinfo:
            await manager.set_offset("local", offset)
        assert excinfo.value.status_code == 422


async def test_refresh_reconciles_stored_local_sink(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, router, _player, _bus = zone_manager
    await manager.activate("local")
    router.sinks = [SinkInfo(name="usb_dac", description="USB DAC")]
    page = await manager.refresh()
    local = next(zone for zone in page.items if zone.id == "local")
    assert local.sink_name == "usb_dac"


async def test_activate_without_any_sink_is_503(
    zone_manager: tuple[ZoneManager, FakeZoneRouter, FakePlayer, EventBus],
) -> None:
    manager, router, _player, _bus = zone_manager
    router.sinks = []
    with pytest.raises(UnavailableError):
        await manager.activate("local")
    router.sinks = [SinkInfo(name="fake_sink")]
    router.fail_activate = True
    with pytest.raises(UnavailableError):
        await manager.activate("local")


# --------------------------------------------------------------------- pipewire


async def test_pipewire_router_parses_sinks() -> None:
    runner = RecordingRunner(
        {
            ("pactl", "list", "short", "sinks"): CommandResult(
                0,
                "0\talsa_output.pci.analog\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tIDLE\n"
                "1\tbluez_output.AA_BB_CC_DD_EE_01.1\tmodule-bluez5-device.c\t"
                "s16le 2ch 48000Hz\tRUNNING\n",
            )
        }
    )
    router = PipeWireRouter(runner=runner)
    sinks = await router.list_sinks()
    assert [sink.name for sink in sinks] == [
        "alsa_output.pci.analog",
        "bluez_output.AA_BB_CC_DD_EE_01.1",
    ]
    assert all(sink.available for sink in sinks)
    assert router.backend == "pactl"


async def test_pipewire_activate_moves_sink_inputs_without_shell() -> None:
    runner = RecordingRunner(
        {
            ("pactl", "list", "short", "sink-inputs"): CommandResult(
                0, "12\t0\tmpv\tprotocol-native.c\tfloat32le 2ch\n"
            )
        }
    )
    router = PipeWireRouter(runner=runner)

    assert await router.activate("usb_dac") is True
    assert [args for args, _timeout in runner.calls] == [
        ("pactl", "set-default-sink", "usb_dac"),
        ("pactl", "list", "short", "sink-inputs"),
        ("pactl", "move-sink-input", "12", "usb_dac"),
    ]
    # Arguments are a list of strings, never a shell command line.
    for args, timeout_s in runner.calls:
        assert isinstance(args, tuple)
        assert all(isinstance(argument, str) for argument in args)
        assert timeout_s > 0


async def test_pipewire_set_volume_uses_percent_argument() -> None:
    runner = RecordingRunner()
    router = PipeWireRouter(runner=runner)

    assert await router.set_volume("usb_dac", 42) is True
    assert runner.calls[0][0] == ("pactl", "set-sink-volume", "usb_dac", "42%")
    with pytest.raises(ValueError):
        await router.set_volume("usb_dac", 101)


async def test_pipewire_absence_falls_back_to_mock() -> None:
    router = PipeWireRouter(executable="definitely-not-a-real-pactl-binary")
    assert router.backend == "mock"
    sinks = await router.list_sinks()
    assert [sink.name for sink in sinks] == ["mock_local"]
    assert await router.activate("mock_local") is True


async def test_pipewire_metacharacters_are_single_arguments(tmp_path: Path) -> None:
    runner = RecordingRunner()
    router = PipeWireRouter(runner=runner)
    hostile = "sink; touch pwned $(id)"

    await router.activate(hostile)
    await router.set_volume(hostile, 5)

    rendered = [argument for args, _timeout in runner.calls for argument in args]
    assert hostile in rendered
    assert not any(argument.startswith("sh ") for argument in rendered)
    assert not (tmp_path / "pwned").exists()
    assert router.backend == "pactl"


# --------------------------------------------------------------------------- API


def _build_app(
    settings: Settings, *, with_manager: bool = True
) -> tuple[FastAPI, dict[str, Any]]:
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.paths.database)
        await database.connect()
        await apply_migrations(database)
        await seed_mock_catalog(database)
        bus = EventBus()
        player = FakePlayer()
        context = SimpleNamespace(
            settings=settings, db=database, bus=bus, player=player
        )
        if with_manager:
            context.zones = ZoneManager(
                settings, bus, database, FakeZoneRouter(), player
            )
        state["context"] = context
        app.state.ctx = context
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(zones_router, prefix="/api/v1")
    from app.main import _register_error_handlers

    _register_error_handlers(app)
    return app, state


def _client(tmp_path: Path, *, pin: str | None = None, with_manager: bool = True):
    settings = make_settings(tmp_path, pin=pin)
    app, state = _build_app(settings, with_manager=with_manager)
    return TestClient(app), state


def test_api_zones_read_is_open(tmp_path: Path) -> None:
    client, _state = _client(tmp_path, pin="1234")
    with client:
        response = client.get("/api/v1/zones")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["id"] for item in payload["items"]] == ["local", "browser", "bt"]
    assert payload["items"][0]["active"] is True


@pytest.mark.skip(reason="PIN feature removed")
def test_api_zone_mutations_require_pin(tmp_path: Path) -> None:
    client, _state = _client(tmp_path, pin="1234")
    with client:
        assert client.post("/api/v1/zones/local/activate").status_code == 401
        assert (
            client.post("/api/v1/zones/local/volume", json={"volume": 10}).status_code
            == 401
        )
        assert (
            client.post("/api/v1/zones/local/offset", json={"offset_ms": 10}).status_code
            == 401
        )
        client.cookies.set(COOKIE_NAME, issue_session(client.app.state.ctx.settings.security))
        assert client.post("/api/v1/zones/local/activate").status_code == 200


def test_api_zone_activate_and_persist(tmp_path: Path) -> None:
    client, _state = _client(tmp_path)
    with client:
        response = client.post("/api/v1/zones/browser/activate")
        assert response.status_code == 200
        assert response.json()["active"] is True
        page = client.get("/api/v1/zones").json()
        assert {item["id"]: item["active"] for item in page["items"]}["browser"] is True


def test_api_zone_volume_and_offset_validation(tmp_path: Path) -> None:
    client, _state = _client(tmp_path)
    with client:
        assert client.post("/api/v1/zones/local/volume", json={"volume": 55}).status_code == 200
        assert client.get("/api/v1/zones").json()["items"][0]["volume"] == 55
        assert client.post("/api/v1/zones/local/volume", json={"volume": 101}).status_code == 422
        assert client.post("/api/v1/zones/local/volume", json={"volume": -1}).status_code == 422
        response = client.post("/api/v1/zones/bt/offset", json={"offset_ms": 300})
        assert response.status_code == 200
        assert response.json()["offset_ms"] == 300
        assert (
            client.post(
                "/api/v1/zones/bt/offset", json={"offset_ms": ZONE_OFFSET_MAX_MS + 1}
            ).status_code
            == 422
        )


def test_api_unknown_zone_is_404(tmp_path: Path) -> None:
    client, _state = _client(tmp_path)
    with client:
        response = client.post("/api/v1/zones/ghost/activate")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_api_falls_back_to_context_wiring(tmp_path: Path) -> None:
    client, state = _client(tmp_path, with_manager=False)
    with client:
        response = client.get("/api/v1/zones")
        assert response.status_code == 200
        assert hasattr(state["context"], "zones")
        activate = client.post("/api/v1/zones/local/activate")
        assert activate.status_code == 200
        assert activate.json()["sink_name"] == "mock_local"


def test_api_without_context_dependencies_is_503(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ctx = SimpleNamespace(settings=settings)
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(zones_router, prefix="/api/v1")
    from app.main import _register_error_handlers

    _register_error_handlers(app)
    with TestClient(app) as client:
        response = client.get("/api/v1/zones")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
