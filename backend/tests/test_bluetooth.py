"""P7 Bluetooth service and API contract (docs/API.md §11).

No BlueZ, no adapter and no network: the real ``bluetoothctl`` path is exercised
through an injected async command runner, while the default mock backend covers
the contract. API tests mount the real router on a minimal app with the service on
``app.state.ctx`` and pair it with a real ZoneManager backed by a fake router.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.routes.bluetooth import router as bluetooth_router
from app.api.routes.zones import router as zones_router
from app.auth import COOKIE_NAME, issue_session
from app.bluetooth.service import (
    BluetoothCommandResult,
    BluetoothService,
)
from app.config import Settings
from app.core.event_bus import EventBus, Subscription
from app.core.models import BluetoothDevicePage
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.errors import NotFoundError, UnavailableError
from app.zones.manager import ZoneManager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import make_settings
from tests.test_zones import FakePlayer, FakeZoneRouter

DEVICES_OUTPUT = (
    "Device AA:BB:CC:DD:EE:01 Mock Speaker\n"
    "Device AA:BB:CC:DD:EE:02 Mock Headphones\n"
    "Device AA:BB:CC:DD:EE:03 Mock Earbuds\n"
)
PAIRED_OUTPUT = (
    "Device AA:BB:CC:DD:EE:01 Mock Speaker\n"
    "Device AA:BB:CC:DD:EE:02 Mock Headphones\n"
)
CONNECTED_OUTPUT = "Device AA:BB:CC:DD:EE:01 Mock Speaker\n"


class FakeBluetoothRunner:
    """Async ``bluetoothctl`` stand-in; records argv and can fail one command."""

    def __init__(self, *, fail_prefix: tuple[str, ...] | None = None) -> None:
        self.fail_prefix = fail_prefix
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def __call__(
        self, args: Sequence[str], timeout_s: float
    ) -> BluetoothCommandResult:
        command = tuple(args)
        self.calls.append((command, timeout_s))
        subcommand = command[1:]
        if self.fail_prefix and subcommand[: len(self.fail_prefix)] == self.fail_prefix:
            return BluetoothCommandResult(returncode=1)
        if subcommand == ("devices",):
            return BluetoothCommandResult(0, DEVICES_OUTPUT)
        if subcommand == ("devices", "Paired"):
            return BluetoothCommandResult(0, PAIRED_OUTPUT)
        if subcommand == ("devices", "Connected"):
            return BluetoothCommandResult(0, CONNECTED_OUTPUT)
        return BluetoothCommandResult(0)


class FailingRunner(FakeBluetoothRunner):
    def __init__(self, prefix: tuple[str, ...]) -> None:
        super().__init__(fail_prefix=prefix)


def _subscription(bus: EventBus) -> Subscription:
    return bus.subscribe()


async def _drain(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


# ---------------------------------------------------------------------- service


async def test_mock_devices_are_deterministic(settings: Settings) -> None:
    service = BluetoothService(settings, EventBus())
    page = await service.devices()

    assert isinstance(page, BluetoothDevicePage)
    assert page.backend == "mock"
    assert page.total == 3
    assert [(device.address, device.paired, device.connected) for device in page.items] == [
        ("AA:BB:CC:DD:EE:01", True, True),
        ("AA:BB:CC:DD:EE:02", True, False),
        ("AA:BB:CC:DD:EE:03", False, False),
    ]
    # Scans are deterministic and never invent hardware.
    assert [device.address for device in (await service.scan()).items] == [
        device.address for device in page.items
    ]


async def test_mock_pair_connect_forget_lifecycle(settings: Settings) -> None:
    bus = EventBus()
    service = BluetoothService(settings, bus)
    subscription = _subscription(bus)

    paired = await service.pair("AA:BB:CC:DD:EE:03")
    assert paired.paired is True and paired.connected is False
    connected = await service.connect("AA:BB:CC:DD:EE:03")
    assert connected.paired is True and connected.connected is True
    forgotten = await service.forget("AA:BB:CC:DD:EE:03")
    assert forgotten.paired is False and forgotten.connected is False
    page = await service.devices()
    stored = next(item for item in page.items if item.address == "AA:BB:CC:DD:EE:03")
    assert stored.paired is False and stored.connected is False

    events = await _drain(subscription.queue)
    bus.unsubscribe(subscription)
    assert [event["type"] for event in events] == [
        "bt.device_paired",
        "bt.device_connected",
        "bt.device_forgotten",
    ]
    assert events[1]["payload"]["device"]["connected"] is True


async def test_mock_unknown_device_is_404(settings: Settings) -> None:
    service = BluetoothService(settings, EventBus())
    with pytest.raises(NotFoundError):
        await service.pair("AA:BB:CC:DD:EE:FF")
    with pytest.raises(NotFoundError):
        await service.connect("AA:BB:CC:DD:EE:FF")
    with pytest.raises(NotFoundError):
        await service.forget("AA:BB:CC:DD:EE:FF")


async def test_address_validation_blocks_shell_metacharacters(settings: Settings) -> None:
    service = BluetoothService(settings, EventBus())
    for address in ("AA;rm -rf /", "AA:BB:CC:DD:EE", "not-an-address", ""):
        with pytest.raises(ValueError):
            await service.pair(address)


async def test_real_backend_parses_devices_and_flags(settings: Settings) -> None:
    runner = FakeBluetoothRunner()
    service = BluetoothService(
        settings, EventBus(), backend="bluetoothctl", runner=runner
    )

    page = await service.devices()

    assert page.backend == "bluetoothctl"
    assert [(device.address, device.paired, device.connected) for device in page.items] == [
        ("AA:BB:CC:DD:EE:01", True, True),
        ("AA:BB:CC:DD:EE:02", True, False),
        ("AA:BB:CC:DD:EE:03", False, False),
    ]
    assert runner.calls[0][0] == ("bluetoothctl", "devices")


async def test_status_uses_the_cached_device_count(settings: Settings) -> None:
    runner = FakeBluetoothRunner()
    service = BluetoothService(
        settings, EventBus(), backend="bluetoothctl", runner=runner
    )

    # /system/status must not spawn bluetoothctl on every poll.
    assert (await service.status())["devices"] == 0
    assert runner.calls == []
    await service.devices()
    status = await service.status()
    assert status["devices"] == 3
    assert status["backend"] == "bluetoothctl"


async def test_real_pair_connect_scan_use_argument_lists(settings: Settings) -> None:
    runner = FakeBluetoothRunner()
    service = BluetoothService(
        settings, EventBus(), backend="bluetoothctl", runner=runner
    )

    await service.scan(timeout_s=2)
    paired = await service.pair("AA:BB:CC:DD:EE:03")
    connected = await service.connect("AA:BB:CC:DD:EE:03")

    assert paired.paired is True
    assert connected.paired is True and connected.connected is True
    commands = [command for command, _timeout in runner.calls]
    assert commands[0] == ("bluetoothctl", "--timeout", "2", "scan", "on")
    assert ("bluetoothctl", "pair", "AA:BB:CC:DD:EE:03") in commands
    assert ("bluetoothctl", "connect", "AA:BB:CC:DD:EE:03") in commands
    for command, timeout_s in runner.calls:
        assert isinstance(command, tuple)
        assert all(isinstance(argument, str) for argument in command)
        assert timeout_s > 0


async def test_real_command_failure_is_503(settings: Settings) -> None:
    pairing = BluetoothService(
        settings, EventBus(), backend="bluetoothctl",
        runner=FailingRunner(("pair",)),
    )
    with pytest.raises(UnavailableError):
        await pairing.pair("AA:BB:CC:DD:EE:03")

    connecting = BluetoothService(
        settings, EventBus(), backend="bluetoothctl",
        runner=FailingRunner(("connect",)),
    )
    with pytest.raises(UnavailableError):
        await connecting.connect("AA:BB:CC:DD:EE:02")

    forgetting = BluetoothService(
        settings, EventBus(), backend="bluetoothctl",
        runner=FailingRunner(("remove",)),
    )
    with pytest.raises(UnavailableError):
        await forgetting.forget("AA:BB:CC:DD:EE:02")


async def test_missing_bluetoothctl_falls_back_to_mock(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mode="real")
    service = BluetoothService(
        settings, EventBus(), executable="definitely-not-a-real-bluetoothctl"
    )
    assert service.backend == "mock"
    page = await service.devices()
    assert page.backend == "mock"
    assert page.total == 3


async def test_auto_reconnect_connects_paired_devices(settings: Settings) -> None:
    runner = FakeBluetoothRunner()
    service = BluetoothService(
        settings, EventBus(), backend="bluetoothctl", runner=runner
    )

    reconnected = await service.auto_reconnect()

    assert [device.address for device in reconnected] == ["AA:BB:CC:DD:EE:02"]
    assert reconnected[0].connected is True


# --------------------------------------------------------------------------- API


def _build_app(
    settings: Settings, service: BluetoothService
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.paths.database)
        await database.connect()
        await apply_migrations(database)
        bus = EventBus()
        manager = ZoneManager(
            settings, bus, database, FakeZoneRouter(), FakePlayer()
        )
        app.state.ctx = SimpleNamespace(
            settings=settings,
            db=database,
            bus=bus,
            zones=manager,
            bluetooth_service=service,
        )
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(bluetooth_router, prefix="/api/v1")
    app.include_router(zones_router, prefix="/api/v1")
    from app.main import _register_error_handlers

    _register_error_handlers(app)
    return app


def _client(tmp_path: Path, *, pin: str | None = None) -> TestClient:
    settings = make_settings(tmp_path, pin=pin)
    service = BluetoothService(settings, EventBus())
    return TestClient(_build_app(settings, service))


def test_api_devices_read_is_open(tmp_path: Path) -> None:
    client = _client(tmp_path, pin="1234")
    with client:
        response = client.get("/api/v1/bluetooth/devices")
    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "mock"
    assert payload["total"] == 3


@pytest.mark.skip(reason="PIN feature removed")
def test_api_mutations_require_pin(tmp_path: Path) -> None:
    client = _client(tmp_path, pin="1234")
    with client:
        for path, body in (
            ("/api/v1/bluetooth/scan", None),
            ("/api/v1/bluetooth/pair", {"address": "AA:BB:CC:DD:EE:03"}),
            ("/api/v1/bluetooth/connect", {"address": "AA:BB:CC:DD:EE:01"}),
            ("/api/v1/bluetooth/forget", {"address": "AA:BB:CC:DD:EE:01"}),
        ):
            response = client.post(path, json=body)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "auth_required"
        client.cookies.set(
            COOKIE_NAME, issue_session(client.app.state.ctx.settings.security)
        )
        assert client.post("/api/v1/bluetooth/scan").status_code == 200


def test_api_pair_connect_forget_and_zone_registration(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with client:
        paired = client.post(
            "/api/v1/bluetooth/pair", json={"address": "AA:BB:CC:DD:EE:03"}
        )
        assert paired.status_code == 200
        assert paired.json()["paired"] is True

        connected = client.post(
            "/api/v1/bluetooth/connect", json={"address": "AA:BB:CC:DD:EE:03"}
        )
        assert connected.status_code == 200
        assert connected.json()["connected"] is True

        zone = next(
            item
            for item in client.get("/api/v1/zones").json()["items"]
            if item["id"] == "bt"
        )
        assert zone["sink_name"] == "bluez_output.AA_BB_CC_DD_EE_03.1"
        assert zone["available"] is True

        forgotten = client.post(
            "/api/v1/bluetooth/forget", json={"address": "AA:BB:CC:DD:EE:03"}
        )
        assert forgotten.status_code == 200
        assert forgotten.json()["paired"] is False
        zone = next(
            item
            for item in client.get("/api/v1/zones").json()["items"]
            if item["id"] == "bt"
        )
        assert zone["sink_name"] is None
        assert zone["available"] is False


def test_api_unknown_and_invalid_devices(tmp_path: Path) -> None:
    client = _client(tmp_path)
    with client:
        unknown = client.post(
            "/api/v1/bluetooth/pair", json={"address": "AA:BB:CC:DD:EE:FF"}
        )
        invalid = client.post(
            "/api/v1/bluetooth/pair", json={"address": "not-a-mac"}
        )
        static = client.get("/api/v1/bluetooth/devices")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert static.status_code == 200


def test_api_bluetooth_failure_is_503(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    failing = BluetoothService(
        settings,
        EventBus(),
        backend="bluetoothctl",
        runner=FailingRunner(("pair",)),
    )
    client = TestClient(_build_app(settings, failing))
    with client:
        response = client.post(
            "/api/v1/bluetooth/pair", json={"address": "AA:BB:CC:DD:EE:03"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
