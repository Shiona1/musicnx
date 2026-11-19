"""P8 Network/Wi-Fi service and API contract (docs/API.md §13).

No NetworkManager, no Wi-Fi radio and no Raspberry Pi are required. The real
``nmcli`` path is exercised through an injected async runner; the default mock
backend covers deterministic status/scan/connect. API tests mount the real router
on a minimal app with the service on ``app.state.ctx``.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import app.api.deps as deps
import pytest
from app.api.routes.network import router as network_router
from app.auth import COOKIE_NAME, issue_session
from app.config import Settings
from app.core.event_bus import EventBus
from app.errors import UnavailableError
from app.network.service import (
    TIMEOUT_CODE,
    NetworkCommandResult,
    NetworkService,
    parse_device_status,
    parse_ip4_address,
    parse_wifi_networks,
    run_network_command,
    split_terse_fields,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import make_settings

DEVICE_STATUS_COMMAND = (
    "-t",
    "-f",
    "DEVICE,TYPE,STATE,CONNECTION",
    "device",
    "status",
)
WIFI_LIST_COMMAND = (
    "-t",
    "-f",
    "active,ssid,signal,security",
    "device",
    "wifi",
    "list",
)
IP_COMMAND = ("-t", "-f", "IP4.ADDRESS", "device", "show")

DEVICE_STATUS_OUTPUT = (
    "wlan0:wifi:connected:Mock\\:Home\n"
    "eth0:ethernet:disconnected:\n"
    "lo:loopback:unmanaged:\n"
)
WIFI_LIST_OUTPUT = (
    "yes:Mock\\:Home:88:WPA2\n"
    "no:Mock\\:Home:44:WPA2\n"
    "no:Guest:71:WPA2\n"
    "no:Cafe:71:--\n"
    "no::55:WPA2\n"
)
IP_OUTPUT = "IP4.ADDRESS[1]:192.168.1.42/24\nIP4.GATEWAY:192.168.1.1\n"


def _contains(command: Sequence[str], prefix: Sequence[str]) -> bool:
    width = len(prefix)
    return any(
        tuple(command[index : index + width]) == tuple(prefix)
        for index in range(len(command) - width + 1)
    )


class FakeNmcliRunner:
    """Async ``nmcli`` stand-in; records argv/timeout and can fail one command."""

    def __init__(
        self,
        *,
        fail_prefix: tuple[str, ...] | None = None,
        fail_returncode: int = 1,
        fail_stderr: str = "nmcli failed",
    ) -> None:
        self.fail_prefix = fail_prefix
        self.fail_returncode = fail_returncode
        self.fail_stderr = fail_stderr
        self.calls: list[tuple[tuple[str, ...], float]] = []

    async def __call__(
        self, args: Sequence[str], timeout_s: float
    ) -> NetworkCommandResult:
        command = tuple(args)
        self.calls.append((command, timeout_s))
        if self.fail_prefix and _contains(command, self.fail_prefix):
            return NetworkCommandResult(
                returncode=self.fail_returncode, stderr=self.fail_stderr
            )
        if command[1:] == DEVICE_STATUS_COMMAND:
            return NetworkCommandResult(returncode=0, stdout=DEVICE_STATUS_OUTPUT)
        if command[1:] == WIFI_LIST_COMMAND:
            return NetworkCommandResult(returncode=0, stdout=WIFI_LIST_OUTPUT)
        if command[1 : 1 + len(IP_COMMAND)] == IP_COMMAND:
            return NetworkCommandResult(returncode=0, stdout=IP_OUTPUT)
        if command[1:] == ("networking", "connectivity", "check"):
            return NetworkCommandResult(returncode=0, stdout="full\n")
        return NetworkCommandResult(returncode=0)


@contextmanager
def _captured_network_logs() -> Iterator[io.StringIO]:
    """Attach a temporary handler to the module logger (it does not propagate)."""

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("musicnx.network.service")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _real_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, mode="real")


# ---------------------------------------------------------------------- parsers


def test_terse_parser_honours_escaped_colons_and_backslashes() -> None:
    assert split_terse_fields(r"yes:Mock\:Home\:Upstairs:88:WPA2") == [
        "yes",
        "Mock:Home:Upstairs",
        "88",
        "WPA2",
    ]
    assert split_terse_fields(r"no:Cafe\\Bar:41:--") == [
        "no",
        r"Cafe\Bar",
        "41",
        "--",
    ]


def test_parse_device_status_rows() -> None:
    devices = parse_device_status(DEVICE_STATUS_OUTPUT)

    assert [(device.device, device.kind, device.connected) for device in devices] == [
        ("wlan0", "wifi", True),
        ("eth0", "ethernet", False),
        ("lo", "loopback", False),
    ]
    assert devices[0].connection == "Mock:Home"


def test_parse_wifi_list_dedupes_hidden_and_sorts_by_signal() -> None:
    networks = parse_wifi_networks(WIFI_LIST_OUTPUT)

    assert [network.ssid for network in networks] == [
        "Mock:Home",
        "Cafe",
        "Guest",
    ]
    assert [network.signal for network in networks] == [88, 71, 71]
    assert networks[0].connected is True
    assert networks[0].security == "WPA2"
    assert networks[1].security is None
    assert all(network.ssid for network in networks)


def test_parse_ip4_address() -> None:
    assert parse_ip4_address(IP_OUTPUT) == "192.168.1.42"
    assert parse_ip4_address("IP4.ADDRESS[1]:10.0.0.5/8\n") == "10.0.0.5"
    assert parse_ip4_address("") is None


# ---------------------------------------------------------------------- mock mode


async def test_mock_status_and_scan_are_deterministic(settings: Settings) -> None:
    service = NetworkService(settings, EventBus())

    first = await service.status()
    second = await service.status()
    scan = await service.scan()

    assert first == second
    assert first.backend == "mock"
    assert first.connected is True
    assert first.interface == "mock0"
    assert first.ssid == "Mock Home"
    assert first.internet is True
    assert scan.backend == "mock"
    assert scan.total == 3
    assert [network.ssid for network in scan.items] == [
        "Mock Home",
        "Mock Guest",
        "Mock Cafe",
    ]
    assert [network.signal for network in scan.items] == [82, 64, 41]
    assert scan.items[0].connected is True


async def test_mock_connect_and_disconnect_update_scan(settings: Settings) -> None:
    service = NetworkService(settings, EventBus())
    secret = "mock-password"

    connected = await service.connect("Mock Guest", secret)

    assert connected.connected is True
    assert connected.ssid == "Mock Guest"
    assert connected.signal == 64
    assert secret not in connected.model_dump_json()
    scan = await service.scan()
    assert next(item for item in scan.items if item.ssid == "Mock Guest").connected is True
    assert next(item for item in scan.items if item.ssid == "Mock Home").connected is False

    disconnected = await service.disconnect()
    assert disconnected.connected is False
    assert disconnected.ssid is None
    assert disconnected.ip is None
    assert disconnected.internet is False
    assert disconnected.interface == "mock0"


async def test_missing_nmcli_falls_back_to_mock(tmp_path: Path) -> None:
    settings = _real_settings(tmp_path)
    service = NetworkService(
        settings, EventBus(), executable="definitely-not-a-real-nmcli"
    )

    assert service.backend == "mock"
    status = await service.status()
    assert status.backend == "mock"
    assert status.connected is True
    assert (await service.scan()).total == 3


async def test_injected_runner_is_a_real_backend_even_in_mock_settings(
    settings: Settings,
) -> None:
    service = NetworkService(settings, EventBus(), runner=FakeNmcliRunner())

    assert service.backend == "nmcli"
    assert (await service.scan()).backend == "nmcli"


# -------------------------------------------------------------------- real runner


async def test_real_status_parses_nmcli_output(tmp_path: Path) -> None:
    runner = FakeNmcliRunner()
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )

    status = await service.status()

    assert status.backend == "nmcli"
    assert status.connected is True
    assert status.interface == "wlan0"
    assert status.ssid == "Mock:Home"
    assert status.signal == 88
    assert status.ip == "192.168.1.42"
    assert status.internet is True
    commands = [command for command, _timeout in runner.calls]
    assert ("nmcli", *DEVICE_STATUS_COMMAND) in commands
    assert ("nmcli", *IP_COMMAND, "wlan0") in commands
    assert ("nmcli", *WIFI_LIST_COMMAND) in commands
    assert all(
        10.0 <= timeout_s <= 20.0 for _command, timeout_s in runner.calls
    )


async def test_real_scan_uses_argument_list_and_sorts(tmp_path: Path) -> None:
    runner = FakeNmcliRunner()
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )

    page = await service.scan()

    assert page.backend == "nmcli"
    assert page.total == 3
    assert [network.ssid for network in page.items] == ["Mock:Home", "Cafe", "Guest"]
    command, timeout_s = runner.calls[0]
    assert command == ("nmcli", *WIFI_LIST_COMMAND)
    assert 10.0 <= timeout_s <= 20.0


async def test_connect_uses_argv_and_never_returns_password(tmp_path: Path) -> None:
    runner = FakeNmcliRunner()
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )
    secret = "p@ss;word with spaces"

    with _captured_network_logs() as logs:
        status = await service.connect("Mock:Home", secret)

    connect_command = next(
        command
        for command, _timeout in runner.calls
        if _contains(command, ("device", "wifi", "connect"))
    )
    assert connect_command == (
        "nmcli",
        "device",
        "wifi",
        "connect",
        "Mock:Home",
        "password",
        secret,
    )
    assert status.connected is True
    assert secret not in status.model_dump_json()
    assert secret not in logs.getvalue()


async def test_connect_failure_exposes_only_returncode(tmp_path: Path) -> None:
    secret = "top-secret-password"
    runner = FakeNmcliRunner(
        fail_prefix=("device", "wifi", "connect"),
        fail_stderr=f"nmcli rejected password {secret}",
    )
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )

    with (
        _captured_network_logs() as logs,
        pytest.raises(UnavailableError) as caught,
    ):
        await service.connect("Mock:Home", secret)

    error = caught.value
    assert error.details == {"returncode": 1}
    assert "connection failed" in error.message
    assert secret not in str(error)
    assert secret not in json.dumps(error.details)
    assert secret not in logs.getvalue()


async def test_ssid_metacharacters_are_passed_as_one_argument(tmp_path: Path) -> None:
    runner = FakeNmcliRunner()
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )
    ssid = "Cafe; touch pwned"
    secret = "p@ss;word"

    await service.connect(ssid, secret)

    connect_command = next(
        command
        for command, _timeout in runner.calls
        if _contains(command, ("device", "wifi", "connect"))
    )
    assert connect_command[4] == ssid
    assert connect_command[6] == secret
    assert len(connect_command) == 7


async def test_open_network_connect_omits_password_option(tmp_path: Path) -> None:
    runner = FakeNmcliRunner()
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )

    await service.connect("Cafe", "")

    connect_command = next(
        command
        for command, _timeout in runner.calls
        if _contains(command, ("device", "wifi", "connect"))
    )
    assert connect_command == ("nmcli", "device", "wifi", "connect", "Cafe")


async def test_command_runner_treats_metacharacters_as_data() -> None:
    value = "Cafe; echo injected"
    result = await run_network_command(
        (sys.executable, "-c", "import sys; print(sys.argv[1])", value),
        timeout_s=10.0,
    )

    assert result.ok is True
    assert result.stdout.strip() == value


async def test_command_runner_kills_timed_out_process() -> None:
    started = time.monotonic()
    result = await run_network_command(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_s=0.2,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == TIMEOUT_CODE
    assert elapsed < 3.0


async def test_scan_failure_is_unavailable(tmp_path: Path) -> None:
    runner = FakeNmcliRunner(fail_prefix=("device", "wifi", "list"))
    service = NetworkService(
        _real_settings(tmp_path), EventBus(), runner=runner
    )

    with pytest.raises(UnavailableError):
        await service.scan()


# --------------------------------------------------------------------------- API


def _build_app(settings: Settings, service: NetworkService | None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ctx = SimpleNamespace(
            settings=settings,
            bus=EventBus(),
            network_service=service,
        )
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(network_router, prefix="/api/v1")
    from app.main import _register_error_handlers

    _register_error_handlers(app)
    return app


def test_api_status_is_open_with_pin_configured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, pin="1234")
    client = TestClient(_build_app(settings, NetworkService(settings, EventBus())))

    with client:
        response = client.get("/api/v1/network/status")

    assert response.status_code == 200
    assert response.json()["backend"] == "mock"
    assert response.json()["connected"] is True


def test_full_app_mounts_network_routes(client: TestClient) -> None:
    assert client.get("/api/v1/network/status").status_code == 200
    scan = client.post("/api/v1/network/scan")
    assert scan.status_code == 200
    assert scan.json()["total"] == 3


@pytest.mark.skip(reason="PIN feature removed")
def test_api_mutations_require_pin(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, pin="1234")
    client = TestClient(_build_app(settings, NetworkService(settings, EventBus())))

    with client:
        for path, body in (
            ("/api/v1/network/scan", None),
            (
                "/api/v1/network/connect",
                {"ssid": "Mock Home", "password": "12345678"},
            ),
            ("/api/v1/network/disconnect", None),
        ):
            response = client.post(path, json=body)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "auth_required"

        client.cookies.set(
            COOKIE_NAME, issue_session(settings.security)
        )
        assert client.post("/api/v1/network/scan").status_code == 200
        connected = client.post(
            "/api/v1/network/connect",
            json={"ssid": "Mock Guest", "password": "12345678"},
        )
        assert connected.status_code == 200
        assert connected.json()["ssid"] == "Mock Guest"
        assert "12345678" not in connected.text
        assert client.post("/api/v1/network/disconnect").status_code == 200


@pytest.mark.skip(reason="PIN feature removed")
def test_api_remote_mutation_without_pin_is_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "client_is_loopback", lambda request: False)
    settings = make_settings(tmp_path)
    client = TestClient(_build_app(settings, NetworkService(settings, EventBus())))

    with client:
        status = client.get("/api/v1/network/status")
        response = client.post("/api/v1/network/scan")

    assert status.status_code == 200
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "pin_required"


def test_api_invalid_connect_body_is_422_and_does_not_echo_password(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    client = TestClient(_build_app(settings, NetworkService(settings, EventBus())))
    secret = "never-echo-this-password"

    with client:
        empty_ssid = client.post(
            "/api/v1/network/connect", json={"ssid": "", "password": secret}
        )
        missing_password = client.post(
            "/api/v1/network/connect", json={"ssid": "Mock Home"}
        )
        too_long_ssid = client.post(
            "/api/v1/network/connect",
            json={"ssid": "x" * 33, "password": secret},
        )

    for response in (empty_ssid, missing_password, too_long_ssid):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        assert secret not in response.text


def test_api_connect_open_network_accepts_empty_password(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    client = TestClient(_build_app(settings, NetworkService(settings, EventBus())))

    with client:
        response = client.post(
            "/api/v1/network/connect",
            json={"ssid": "Mock Cafe", "password": ""},
        )

    assert response.status_code == 200
    assert response.json()["ssid"] == "Mock Cafe"


def test_api_failure_is_503_without_password(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    secret = "not-in-the-response"
    runner = FakeNmcliRunner(
        fail_prefix=("device", "wifi", "connect"),
        fail_stderr=f"password {secret}",
    )
    client = TestClient(
        _build_app(
            settings,
            NetworkService(settings, EventBus(), backend="nmcli", runner=runner),
        )
    )

    with client:
        response = client.post(
            "/api/v1/network/connect",
            json={"ssid": "Mock Home", "password": secret},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    assert secret not in response.text


def test_api_scan_failure_is_503(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runner = FakeNmcliRunner(fail_prefix=("device", "wifi", "list"))
    client = TestClient(
        _build_app(
            settings,
            NetworkService(settings, EventBus(), backend="nmcli", runner=runner),
        )
    )

    with client:
        response = client.post("/api/v1/network/scan")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


def test_api_status_failure_is_503(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runner = FakeNmcliRunner(fail_prefix=DEVICE_STATUS_COMMAND)
    client = TestClient(
        _build_app(
            settings,
            NetworkService(settings, EventBus(), backend="nmcli", runner=runner),
        )
    )

    with client:
        response = client.get("/api/v1/network/status")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


def test_api_falls_back_to_building_a_service_and_keeps_state(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    app = _build_app(settings, None)
    ctx = SimpleNamespace(settings=settings, bus=EventBus())

    with TestClient(app) as client:
        # Replace the lifespan context with one that has no service attribute at
        # all, so the router exercises its lazy fallback.
        client.app.state.ctx = ctx
        assert client.get("/api/v1/network/status").status_code == 200
        connected = client.post(
            "/api/v1/network/connect",
            json={"ssid": "Mock Guest", "password": "12345678"},
        )
        status = client.get("/api/v1/network/status")

    assert connected.status_code == 200
    assert status.json()["ssid"] == "Mock Guest"
    assert getattr(ctx, "network_service", None) is not None
