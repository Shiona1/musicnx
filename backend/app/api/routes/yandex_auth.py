"""Yandex Music authentication endpoints (P3, docs/API.md §6.1).

The router is deliberately thin: it exposes the frozen contract and delegates
every secret-bearing operation to the auth manager wired on ``AppContext``.
Device Flow mutation endpoints require PIN/loopback via
``require_mutation_access``; read-only status endpoints stay open.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_context, require_mutation_access
from app.core.models import YandexAuthStatus, YandexDeviceStart, YandexDeviceStatus
from app.integrations.yandex_music.auth import MockYandexAuthManager, YandexAuthManager

router = APIRouter(prefix="/yandex/auth", tags=["yandex"])

AuthManager = YandexAuthManager | MockYandexAuthManager


class ManualTokenRequest(BaseModel):
    """Manual Yandex OAuth token pasted by the user; never logged or echoed."""

    token: str = Field(min_length=1, max_length=4096)


def get_yandex_auth(request: Request) -> AuthManager:
    """Resolve the manager from ``request.app.state.ctx.yandex_auth``.

    Kept as a separate dependency so tests can swap ``app.state.ctx`` for a
    lightweight object exposing ``settings``, ``pin_limiter`` and ``yandex_auth``.
    """

    context = get_context(request)
    manager = getattr(context, "yandex_auth", None)
    if manager is None:
        raise RuntimeError("Yandex authentication manager is not wired into AppContext")
    return manager


@router.post(
    "/device/start",
    response_model=YandexDeviceStart,
    dependencies=[Depends(require_mutation_access)],
)
async def start_device_flow(
    manager: Annotated[AuthManager, Depends(get_yandex_auth)],
) -> YandexDeviceStart:
    """Start OAuth Device Flow; the device code never leaves the server."""

    return await manager.start_device_flow()


@router.get("/device/status", response_model=YandexDeviceStatus)
async def device_status(
    manager: Annotated[AuthManager, Depends(get_yandex_auth)],
    flow_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> YandexDeviceStatus:
    return await manager.device_status(flow_id)


@router.post(
    "/token",
    response_model=YandexAuthStatus,
    dependencies=[Depends(require_mutation_access)],
)
async def set_manual_token(
    payload: ManualTokenRequest,
    manager: Annotated[AuthManager, Depends(get_yandex_auth)],
) -> YandexAuthStatus:
    return await manager.set_manual_token(payload.token)


@router.post(
    "/logout",
    response_model=YandexAuthStatus,
    dependencies=[Depends(require_mutation_access)],
)
async def logout(
    manager: Annotated[AuthManager, Depends(get_yandex_auth)],
) -> YandexAuthStatus:
    await manager.logout()
    return await manager.status()


@router.get("/status", response_model=YandexAuthStatus)
async def status(
    manager: Annotated[AuthManager, Depends(get_yandex_auth)],
) -> YandexAuthStatus:
    return await manager.status()
