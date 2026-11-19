"""Network / Wi-Fi API — P8, frozen contract in docs/API.md §13.

``GET /network/status`` is an open read. Scan/connect/disconnect are mutations
guarded by ``require_mutation_access`` (valid PIN session, or loopback when no
PIN is configured).

The service is looked up as ``ctx.network_service``; the router also accepts a
full service stored as ``ctx.network`` and, when the shared context still holds
only the old status-only mock adapter, lazily builds :class:`NetworkService` from
``ctx.settings``/``ctx.bus`` and caches it. Connecting therefore does not require
a context ordering change before the shared wiring is updated.

A Wi-Fi password is accepted as a Pydantic ``SecretStr``, passed straight to the
service and never echoed by a response, log or error. The mount point lives in
the shared ``app/main.py`` and is intentionally not edited here; the integration
note supplied with this module documents the one-line include::

    app.include_router(network.router, prefix="/api/v1")
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.api.deps import get_context, require_mutation_access
from app.core.models import NetworkStatus, WifiScanPage
from app.errors import AppError, UnavailableError
from app.network.service import NetworkService, validate_ssid

router = APIRouter(prefix="/network", tags=["network"])


class WifiConnectRequest(BaseModel):
    """Body of ``POST /network/connect`` (docs/API.md §13)."""

    ssid: str = Field(min_length=1, max_length=32)
    # Empty is valid for an open network; the field itself stays required.
    password: SecretStr = Field(max_length=128)

    @field_validator("ssid")
    @classmethod
    def _valid_ssid(cls, value: str) -> str:
        return validate_ssid(value)


def _service(request: Request) -> NetworkService:
    context = get_context(request)
    service = getattr(context, "network_service", None)
    if service is None:
        service = getattr(request.app.state, "network_service", None)
    if service is None:
        candidate = getattr(context, "network", None)
        if candidate is not None and callable(getattr(candidate, "scan", None)):
            service = candidate
    if service is None:
        settings = getattr(context, "settings", None)
        bus = getattr(context, "bus", None)
        if settings is None or bus is None:
            raise UnavailableError("network service is not configured")
        service = NetworkService(settings, bus)
        _cache_service(context, request.app.state, service)
    return service


def _cache_service(context: Any, state: Any, service: NetworkService) -> None:
    """Best-effort cache so a lazily built mock keeps its connection state."""

    for target in (context, state):
        try:
            target.network_service = service
        except (AttributeError, TypeError):  # pragma: no cover - frozen contexts
            pass


def _invalid_request() -> AppError:
    # Keep the body generic: it must not reflect SSID/password input.
    return AppError(
        "invalid Wi-Fi network parameters",
        code="validation_error",
        status_code=422,
    )


@router.get("/status", response_model=NetworkStatus)
async def network_status(request: Request) -> NetworkStatus:
    """Current backend, connection, interface, IP, SSID/signal and internet."""

    return await _service(request).status()


@router.post(
    "/scan",
    response_model=WifiScanPage,
    dependencies=[Depends(require_mutation_access)],
)
async def scan_wifi_networks(request: Request) -> WifiScanPage:
    """Scan nearby Wi-Fi networks; credentials are never part of the result."""

    return await _service(request).scan()


@router.post(
    "/connect",
    response_model=NetworkStatus,
    dependencies=[Depends(require_mutation_access)],
)
async def connect_wifi_network(
    request: Request, payload: WifiConnectRequest
) -> NetworkStatus:
    """Join a network; the password is not logged, stored or returned."""

    try:
        return await _service(request).connect(
            payload.ssid, payload.password.get_secret_value()
        )
    except ValueError:
        raise _invalid_request() from None


@router.post(
    "/disconnect",
    response_model=NetworkStatus,
    dependencies=[Depends(require_mutation_access)],
)
async def disconnect_wifi_network(request: Request) -> NetworkStatus:
    """Disconnect the active Wi-Fi interface."""

    return await _service(request).disconnect()
