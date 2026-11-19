"""Contract tests for the P7 zone and Bluetooth fixtures.

The JSON files under ``tests/fixtures/zones`` are canned answers for
``GET /zones``, ``GET /bluetooth/devices`` and single DTOs. They must mirror the
core DTOs (``AudioZone``/``ZonePage``/``BluetoothDevice``/``BluetoothDevicePage``)
from docs/API.md §11, and they must carry no secret or pairing material.

The page fixture has to stay coherent with the standalone fixtures: every item
of ``zones.json`` is the same object as one ``zone_*.json`` file, and the same
holds for ``bluetooth_devices.json`` and ``bluetooth_device_*.json``. That keeps
the fixtures usable both as an API page and as per-object samples.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from app.core.models import (
    AudioZone,
    BluetoothDevice,
    BluetoothDevicePage,
    ZonePage,
)
from pydantic import BaseModel

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "zones"

#: Fixtures validated directly by one core DTO.
MODEL_FIXTURES: dict[str, type[BaseModel]] = {
    "zones.json": ZonePage,
    "zone_local.json": AudioZone,
    "zone_browser.json": AudioZone,
    "zone_bt.json": AudioZone,
    "bluetooth_devices.json": BluetoothDevicePage,
    "bluetooth_device_connected.json": BluetoothDevice,
    "bluetooth_device_available.json": BluetoothDevice,
    "bluetooth_device_discovered.json": BluetoothDevice,
}

ZONE_FIXTURES = ("zone_local.json", "zone_browser.json", "zone_bt.json")
DEVICE_FIXTURES = (
    "bluetooth_device_connected.json",
    "bluetooth_device_available.json",
    "bluetooth_device_discovered.json",
)
EXPECTED_FIXTURES = frozenset(MODEL_FIXTURES)

ALLOWED_KINDS = frozenset({"local_sink", "browser", "bt"})
SECRET_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_", "pin_hash")
FORBIDDEN_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "device_code",
    "authorization",
    "pin",
    "pin_hash",
    "password",
}
MAC_ADDRESS = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")
ZONE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict), f"{name} must be a JSON object"
    return payload


def fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


def _iter_keys(value: Any) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_keys(item)


def _validated(name: str, model: type[BaseModel]) -> Any:
    return model.model_validate(load_fixture(name))


# --------------------------------------------------------------------------- #
# Shape and secrecy
# --------------------------------------------------------------------------- #


def test_fixture_set_is_exactly_the_p7_contract() -> None:
    assert {path.name for path in fixture_paths()} == EXPECTED_FIXTURES


def test_no_secret_markers_in_any_fixture() -> None:
    assert fixture_paths(), "no fixtures found"
    for path in fixture_paths():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in SECRET_MARKERS:
            assert marker not in lowered, f"{path.name} leaks {marker!r}"
        for key in _iter_keys(json.loads(text)):
            assert key.lower() not in FORBIDDEN_KEYS, (
                f"{path.name} contains forbidden field {key!r}"
            )


@pytest.mark.parametrize(("name", "model"), MODEL_FIXTURES.items(), ids=MODEL_FIXTURES.keys())
def test_fixture_matches_core_model(name: str, model: type[BaseModel]) -> None:
    payload = load_fixture(name)
    validated = model.model_validate(payload)
    # The fixture must survive the same JSON round-trip the API performs.
    assert json.loads(validated.model_dump_json()) == payload


# --------------------------------------------------------------------------- #
# AudioZone / ZonePage invariants
# --------------------------------------------------------------------------- #


def test_zone_page_is_the_documented_shape() -> None:
    payload = load_fixture("zones.json")
    assert set(payload) == {"items", "total", "backend", "degraded"}, payload
    assert payload["total"] == len(payload["items"])
    assert payload["items"], "the zone page must not be empty"


def test_zone_page_has_unique_ids_and_one_active_zone() -> None:
    page = _validated("zones.json", ZonePage)
    ids = [zone.id for zone in page.items]
    assert len(set(ids)) == len(ids), f"duplicate zone ids: {ids}"
    assert all(ZONE_ID.match(zone.id) for zone in page.items), ids

    # docs/API.md §11: activation switches the output; at most one zone is active.
    assert sum(zone.active for zone in page.items) == 1, page.items


@pytest.mark.parametrize("name", ZONE_FIXTURES)
def test_zone_fixture_uses_the_documented_kinds(name: str) -> None:
    zone = _validated(name, AudioZone)
    assert zone.kind in ALLOWED_KINDS
    assert zone.name.strip(), f"{name}: a zone needs a human-readable name"
    assert 0 <= zone.volume <= 100, f"{name}: volume must be an integer percent"
    assert isinstance(zone.offset_ms, int), f"{name}: offset_ms must be an integer"
    assert isinstance(zone.active, bool)
    assert isinstance(zone.available, bool)


def test_sink_less_browser_zone_and_sink_backed_hardware_zones() -> None:
    browser = _validated("zone_browser.json", AudioZone)
    assert browser.kind == "browser"
    # A browser zone has no PipeWire sink: clients play the stream themselves.
    assert browser.sink_name is None

    for name in ("zone_local.json", "zone_bt.json"):
        zone = _validated(name, AudioZone)
        assert zone.kind in {"local_sink", "bt"}
        assert zone.sink_name and zone.sink_name.strip(), (
            f"{name}: {zone.kind} must name a PipeWire sink"
        )


def test_zone_page_items_match_the_standalone_fixtures() -> None:
    page = _validated("zones.json", ZonePage)
    expected = {
        zone.id: zone.model_dump()
        for zone in (AudioZone.model_validate(load_fixture(name)) for name in ZONE_FIXTURES)
    }
    assert {zone.id: zone.model_dump() for zone in page.items} == expected
    active = next(zone for zone in page.items if zone.active)
    assert active.kind == "local_sink", (
        "the canned page should keep the wired local sink active"
    )


# --------------------------------------------------------------------------- #
# BluetoothDevice / BluetoothDevicePage invariants
# --------------------------------------------------------------------------- #


def test_bluetooth_page_is_the_documented_shape() -> None:
    payload = load_fixture("bluetooth_devices.json")
    assert set(payload) == {"items", "total", "backend"}, payload
    assert payload["total"] == len(payload["items"])
    assert payload["backend"], "the page must name the BT backend"


def test_bluetooth_devices_have_unique_normalized_addresses() -> None:
    page = _validated("bluetooth_devices.json", BluetoothDevicePage)
    assert page.items, "the device page must not be empty"
    assert page.backend, "the page must name the BT backend"
    addresses = [device.address for device in page.items]
    assert len(set(addresses)) == len(addresses), f"duplicate addresses: {addresses}"
    for address in addresses:
        assert MAC_ADDRESS.match(address), f"{address!r} is not an uppercase MAC address"


@pytest.mark.parametrize("name", DEVICE_FIXTURES)
def test_connected_device_is_always_paired(name: str) -> None:
    device = _validated(name, BluetoothDevice)
    assert device.paired or not device.connected, (
        f"{name}: a connected device must be paired"
    )
    assert device.address, f"{name}: address is required"
    if device.name is not None:
        assert device.name.strip(), f"{name}: an optional name must not be blank"


def test_bluetooth_page_mixes_paired_and_discovered_devices() -> None:
    page = _validated("bluetooth_devices.json", BluetoothDevicePage)
    # The canned page must exercise both branches of the UI/API contract.
    assert any(device.paired for device in page.items)
    assert any(not device.paired for device in page.items)
    assert any(device.connected for device in page.items)


def test_bluetooth_page_items_match_the_standalone_fixtures() -> None:
    page = _validated("bluetooth_devices.json", BluetoothDevicePage)
    expected = {
        device.address: device.model_dump()
        for device in (
            BluetoothDevice.model_validate(load_fixture(name)) for name in DEVICE_FIXTURES
        )
    }
    assert {device.address: device.model_dump() for device in page.items} == expected
    assert len(page.items) == len(expected), "the page and standalone fixtures disagree"
