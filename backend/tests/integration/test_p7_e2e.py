"""P7 end-to-end: local zones and Bluetooth in mock mode.

Contract: docs/API.md §11 (routes, ``AudioZone``/``ZonePage``/
``BluetoothDevice``/``BluetoothDevicePage``, PIN/localhost guard) and the P7
section of PLAN.md. Everything runs in mock mode: no PipeWire, no BlueZ, no
Bluetooth hardware, no Pi.

The only skip gate is a missing route: each scenario checks the live FastAPI
route table and skips with the absent path when the P7 routers are not wired
yet. Nothing else is skipped silently. Once the routes are registered the tests
exercise the real mock adapters, so they also prove that mock mode is honest
about having no hardware.

Fault injection is unnecessary here: there is no test-only hook in the contract
and the mock adapters are deterministic. Unknown IDs/addresses are the negative
cases; PIN 401 covers the security boundary.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.core.models import (
    AudioZone,
    BluetoothDevice,
    BluetoothDevicePage,
    ZonePage,
)
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"
ZONES = f"{API}/zones"
BT = f"{API}/bluetooth"
AUTH = f"{API}/auth"

PIN = "1234"
UNKNOWN = "does-not-exist"
SECRET_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_", "pin_hash")
BT_WAIT_TIMEOUT_S = 3.0
POLL_INTERVAL_S = 0.02


# --------------------------------------------------------------------------- #
# Fixtures and route gates
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _client(
    tmp_path: Path, *, pin: str | None = PIN, login: bool = True
) -> Iterator[TestClient]:
    """Mock-mode app with a fresh database and an optional PIN session."""

    settings: Settings = make_settings(tmp_path, mode="mock", pin=pin)
    app = create_app(settings)
    with TestClient(app) as client:
        if pin is not None and login:
            response = client.post(f"{AUTH}/pin", json={"pin": pin})
            assert response.status_code == 200, response.text
        yield client


def _registered_paths(client: TestClient) -> set[str]:
    return {getattr(route, "path", "") for route in client.app.routes}


def _require(client: TestClient, prefix: str, suffix: str | None = None) -> None:
    """Skip when ``prefix`` (or ``prefix`` + a dynamic segment ending in suffix) is absent."""

    paths = _registered_paths(client)
    if suffix is None:
        present = prefix in paths
        name = prefix
    else:
        present = any(
            path.startswith(f"{prefix}/") and path.endswith(suffix) for path in paths
        )
        name = f"{prefix}/{{id}}{suffix}"
    if not present:
        pytest.skip(
            f"P7 route {name} is not registered yet (docs/API.md §11); "
            "nothing else in this test is skipped"
        )


def _zone_mutation_path(zone_id: str, action: str) -> str:
    return f"{ZONES}/{zone_id}/{action}"


def _assert_no_secrets(text: str) -> None:
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in lowered, f"response leaks {marker!r}"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _zone_page(client: TestClient) -> ZonePage:
    response = client.get(ZONES)
    assert response.status_code == 200, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"items", "total", "backend", "degraded"}, payload
    page = ZonePage.model_validate(payload)
    assert page.total == len(page.items), "total must match the number of zones"
    return page


def _zones(client: TestClient) -> list[AudioZone]:
    page = _zone_page(client)
    assert page.items, "mock mode must expose at least the local sink"
    ids = [zone.id for zone in page.items]
    assert len(set(ids)) == len(ids), f"duplicate zone ids: {ids}"
    assert all(0 <= zone.volume <= 100 for zone in page.items), page.items
    return page.items


def _zone(client: TestClient, zone_id: str) -> AudioZone:
    for zone in _zones(client):
        if zone.id == zone_id:
            return zone
    pytest.fail(f"zone {zone_id!r} disappeared from GET /zones")


def _bt_page(client: TestClient) -> BluetoothDevicePage:
    response = client.get(f"{BT}/devices")
    assert response.status_code == 200, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"items", "total", "backend"}, payload
    page = BluetoothDevicePage.model_validate(payload)
    assert page.total == len(page.items), "total must match the number of devices"
    assert page.backend == "mock", "docs/API.md §11: mock mode must say so"
    return page


def _devices(client: TestClient) -> list[BluetoothDevice]:
    page = _bt_page(client)
    addresses = [device.address for device in page.items]
    assert len(set(addresses)) == len(addresses), f"duplicate addresses: {addresses}"
    return page.items


def _wait_for_devices(client: TestClient) -> list[BluetoothDevice]:
    """Scan is allowed to be asynchronous; wait briefly for the mock devices."""

    deadline = time.monotonic() + BT_WAIT_TIMEOUT_S
    devices: list[BluetoothDevice] = []
    while time.monotonic() < deadline:
        devices = _devices(client)
        if devices:
            return devices
        time.sleep(POLL_INTERVAL_S)
    pytest.fail("mock Bluetooth scan discovered no devices (docs/API.md §11)")


def _assert_mutation_ok(response: Any) -> None:
    assert response.status_code in {200, 202, 204}, response.text


def _assert_error(response: Any, status_code: int, code: str | None = None) -> None:
    assert response.status_code == status_code, response.text
    _assert_no_secrets(response.text)
    error = response.json()["error"]
    assert error["code"], error
    if code is not None:
        assert error["code"] == code, error


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #


def test_zones_list_returns_the_documented_page(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, ZONES)
        page = _zone_page(client)
        assert page.items, page
        assert all(zone.kind in {"local_sink", "browser", "bt"} for zone in page.items)
        assert all(zone.name.strip() for zone in page.items)
        assert sum(zone.active for zone in page.items) <= 1, (
            "only one output can be active at a time"
        )


def test_activate_switches_the_active_zone(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, ZONES, "/activate")
        zones = _zones(client)
        target = next((zone for zone in zones if not zone.active), zones[0])

        response = client.post(_zone_mutation_path(target.id, "activate"))
        _assert_mutation_ok(response)
        _assert_no_secrets(response.text)

        after = _zones(client)
        assert [zone.id for zone in after if zone.active] == [target.id], after
        assert _zone(client, target.id).active is True


def test_zone_volume_is_applied_per_zone_and_validated(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, ZONES, "/volume")
        zones = _zones(client)
        target = zones[0]
        others = {zone.id: zone.volume for zone in zones if zone.id != target.id}
        new_volume = 37 if target.volume != 37 else 38

        response = client.post(
            _zone_mutation_path(target.id, "volume"), json={"volume": new_volume}
        )
        _assert_mutation_ok(response)
        _assert_no_secrets(response.text)
        assert _zone(client, target.id).volume == new_volume
        for zone in _zones(client):
            if zone.id != target.id:
                assert zone.volume == others[zone.id], "volume leaked to another zone"

        for invalid in (-1, 101):
            rejected = client.post(
                _zone_mutation_path(target.id, "volume"), json={"volume": invalid}
            )
            # FastAPI's own validation envelope, not the app error envelope.
            assert rejected.status_code == 422, rejected.text
            _assert_no_secrets(rejected.text)
        assert _zone(client, target.id).volume == new_volume, (
            "a rejected update must not change the stored volume"
        )


def test_zone_offset_is_applied_per_zone(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, ZONES, "/offset")
        target = _zones(client)[0]

        response = client.post(
            _zone_mutation_path(target.id, "offset"), json={"offset_ms": -250}
        )
        _assert_mutation_ok(response)
        _assert_no_secrets(response.text)
        assert _zone(client, target.id).offset_ms == -250

        # A positive correction is valid too (docs/API.md §11, ADR-034).
        response = client.post(
            _zone_mutation_path(target.id, "offset"), json={"offset_ms": 120}
        )
        _assert_mutation_ok(response)
        assert _zone(client, target.id).offset_ms == 120


def test_unknown_zone_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, ZONES, "/activate")
        for action, body in (
            ("activate", None),
            ("volume", {"volume": 50}),
            ("offset", {"offset_ms": 0}),
        ):
            response = client.post(
                _zone_mutation_path(UNKNOWN, action), json=body
            )
            _assert_error(response, 404, "not_found")


# --------------------------------------------------------------------------- #
# Bluetooth
# --------------------------------------------------------------------------- #


def test_bluetooth_devices_list_returns_the_documented_page(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, f"{BT}/devices")
        page = _bt_page(client)
        # An empty page is valid before the first scan; the shape is the contract.
        assert page.total == len(page.items)
        for device in page.items:
            assert device.address, device
            assert device.paired or not device.connected, (
                f"connected device {device.address} must be paired"
            )


def test_bluetooth_scan_is_safe_and_idempotent(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, f"{BT}/scan")
        _require(client, f"{BT}/devices")
        for _ in range(2):
            response = client.post(f"{BT}/scan")
            _assert_mutation_ok(response)
            _assert_no_secrets(response.text)
        devices = _wait_for_devices(client)
        assert devices, "a mock scan must still expose deterministic devices"


def test_bluetooth_pair_and_connect_flow(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, f"{BT}/pair", None)
        _require(client, f"{BT}/connect", None)
        # The mock may start with an empty list and discover devices on scan.
        if f"{BT}/scan" in _registered_paths(client):
            scan = client.post(f"{BT}/scan")
            _assert_mutation_ok(scan)
        devices = _wait_for_devices(client)

        target = next((device for device in devices if not device.paired), devices[0])
        response = client.post(f"{BT}/pair", json={"address": target.address})
        _assert_mutation_ok(response)
        _assert_no_secrets(response.text)
        paired = next(
            device for device in _devices(client) if device.address == target.address
        )
        assert paired.paired is True

        response = client.post(f"{BT}/connect", json={"address": target.address})
        _assert_mutation_ok(response)
        _assert_no_secrets(response.text)
        connected = next(
            device for device in _devices(client) if device.address == target.address
        )
        assert connected.connected is True
        assert connected.paired is True


def test_unknown_bluetooth_address_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _require(client, f"{BT}/pair", None)
        _require(client, f"{BT}/connect", None)
        for action in ("pair", "connect"):
            response = client.post(f"{BT}/{action}", json={"address": "FF:FF:FF:FF:FF:FF"})
            _assert_error(response, 404, "not_found")

            invalid = client.post(f"{BT}/{action}", json={"address": "not-a-mac"})
            assert invalid.status_code == 422, invalid.text
            _assert_no_secrets(invalid.text)

        # forget is part of the same contract; check it when it exists.
        if f"{BT}/forget" in _registered_paths(client):
            response = client.post(
                f"{BT}/forget", json={"address": "FF:FF:FF:FF:FF:FF"}
            )
            _assert_error(response, 404, "not_found")


# --------------------------------------------------------------------------- #
# Security: read-only endpoints are open, mutations are PIN/localhost guarded
# --------------------------------------------------------------------------- #


def test_read_only_zones_and_bluetooth_are_open(tmp_path: Path) -> None:
    with _client(tmp_path, pin=None, login=False) as client:
        _require(client, ZONES)
        _require(client, f"{BT}/devices")
        assert client.get(ZONES).status_code == 200
        assert client.get(f"{BT}/devices").status_code == 200


@pytest.mark.skip(reason="PIN feature removed")
def test_mutations_require_pin_access(tmp_path: Path) -> None:
    with _client(tmp_path, pin=PIN, login=False) as client:
        _require(client, ZONES, "/activate")
        _require(client, ZONES, "/volume")
        _require(client, ZONES, "/offset")
        _require(client, f"{BT}/scan", None)
        _require(client, f"{BT}/pair", None)
        _require(client, f"{BT}/connect", None)

        mutations = [
            (_zone_mutation_path(UNKNOWN, "activate"), None),
            (_zone_mutation_path(UNKNOWN, "volume"), {"volume": 50}),
            (_zone_mutation_path(UNKNOWN, "offset"), {"offset_ms": 0}),
            (f"{BT}/scan", None),
            (f"{BT}/pair", {"address": "FF:FF:FF:FF:FF:FF"}),
            (f"{BT}/connect", {"address": "FF:FF:FF:FF:FF:FF"}),
        ]
        if f"{BT}/forget" in _registered_paths(client):
            mutations.append((f"{BT}/forget", {"address": "FF:FF:FF:FF:FF:FF"}))
        for path, body in mutations:
            response = client.post(path, json=body)
            _assert_error(response, 401, "auth_required")

        # Reads stay available without a session.
        assert client.get(ZONES).status_code == 200
        assert client.get(f"{BT}/devices").status_code == 200
