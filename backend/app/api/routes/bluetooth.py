"""Bluetooth API — P7, frozen contract in docs/API.md §11.

Device listing is an open read; scan/pair/connect/forget are mutations guarded by
``require_mutation_access`` (PIN session, or loopback without a PIN).

The service is looked up as ``ctx.bluetooth_service`` and falls back to
``ctx.bluetooth`` when that object is a device service, then to a lazily built
service from ``ctx.settings/bus``. A successful connect registers the A2DP sink
as the ``bt`` zone through the zone manager (when one is configured); forget
detaches it again.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.api.routes.zones import resolve_zone_manager
from app.bluetooth.service import (
    ADDRESS_PATTERN,
    BluetoothService,
    create_bluetooth_service,
)
from app.core.models import BluetoothDevice, BluetoothDevicePage
from app.errors import NotFoundError, UnavailableError
from app.logging_setup import get_logger
from app.zones.manager import BT_ZONE_ID

router = APIRouter(prefix="/bluetooth", tags=["bluetooth"])
logger = get_logger("api.bluetooth")


class BluetoothAddressRequest(BaseModel):
    """Body of the pair/connect/forget routes (docs/API.md §11)."""

    address: str = Field(pattern=ADDRESS_PATTERN, max_length=17)


def _service(request: Request) -> BluetoothService:
    context = get_context(request)
    service = getattr(context, "bluetooth_service", None)
    if service is None:
        candidate = getattr(context, "bluetooth", None)
        if candidate is not None and callable(getattr(candidate, "devices", None)):
            service = candidate
    if service is None:
        settings = getattr(context, "settings", None)
        bus = getattr(context, "bus", None)
        if settings is None or bus is None:
            raise UnavailableError("bluetooth is not configured")
        service = create_bluetooth_service(settings, bus)
        try:
            context.bluetooth_service = service
        except (AttributeError, TypeError):  # pragma: no cover - frozen contexts
            pass
    return service


async def _attach_and_activate(context: Any, device: BluetoothDevice) -> None:
    """Register the sink and make it the active output.

    A user who presses "connect" expects audio on the speaker, not just a new
    row in the list. If the sink has not appeared yet, the zone activation is
    best-effort: the connection itself already succeeded.
    """

    try:
        manager = resolve_zone_manager(context)
    except UnavailableError:
        return
    try:
        await manager.attach_bluetooth(device)
        if not await manager.wait_bluetooth_sink(device):
            await context.bus.publish(
                "system.warning",
                {
                    "code": "bt_no_a2dp_sink",
                    "message": (
                        "Bluetooth connected, but PipeWire did not create an "
                        "A2DP sink; check wireplumber/pipewire on the Pi"
                    ),
                },
            )
            return
        await manager.activate(BT_ZONE_ID)
    except (NotFoundError, UnavailableError, ValueError) as exc:
        logger.warning("bluetooth zone activation failed: %s", exc)


async def _detach_zone(context: Any) -> None:
    try:
        manager = resolve_zone_manager(context)
    except UnavailableError:
        return
    await manager.detach_bluetooth()


@router.get("/devices", response_model=BluetoothDevicePage)
async def list_bluetooth_devices(request: Request) -> BluetoothDevicePage:
    """Known devices with pairing/connection flags and the active backend."""

    return await _service(request).devices()


@router.post(
    "/scan",
    response_model=BluetoothDevicePage,
    dependencies=[Depends(require_mutation_access)],
)
async def scan_bluetooth_devices(request: Request) -> BluetoothDevicePage:
    """Run a bounded BlueZ discovery and return the refreshed device list."""

    return await _service(request).scan()


@router.post(
    "/pair",
    response_model=BluetoothDevice,
    dependencies=[Depends(require_mutation_access)],
)
async def pair_bluetooth_device(
    request: Request, payload: BluetoothAddressRequest
) -> BluetoothDevice:
    """Pair a discovered device; BlueZ owns any PIN/passkey prompt."""

    return await _service(request).pair(payload.address)


@router.post(
    "/connect",
    response_model=BluetoothDevice,
    dependencies=[Depends(require_mutation_access)],
)
async def connect_bluetooth_device(
    request: Request, payload: BluetoothAddressRequest
) -> BluetoothDevice:
    """Connect a paired device and register its A2DP sink as the ``bt`` zone."""

    device = await _service(request).connect(payload.address)
    if device.connected:
        await _attach_and_activate(get_context(request), device)
    return device


@router.post(
    "/forget",
    response_model=BluetoothDevice,
    dependencies=[Depends(require_mutation_access)],
)
async def forget_bluetooth_device(
    request: Request, payload: BluetoothAddressRequest
) -> BluetoothDevice:
    """Remove the pairing and detach the A2DP zone sink."""

    device = await _service(request).forget(payload.address)
    await _detach_zone(get_context(request))
    return device
