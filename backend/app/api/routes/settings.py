"""Settings API — P8, frozen contract in docs/API.md §12.

``GET /settings`` is open on the LAN (read-only, ADR-021). ``PUT /settings`` is
a mutation guarded by the shared ``require_mutation_access`` dependency (valid
PIN session, or loopback when no PIN is configured).

The service is looked up from the application context (``ctx.settings``,
``ctx.settings_repo``, ``ctx.player``) on every request, so this router can be
mounted without changing the shared context wiring; the integrator only adds
``app.include_router(settings_routes.router, prefix="/api/v1")`` in
``app/main.py``. The route module must be imported under an alias there because
``create_app(settings: Settings | None)`` already uses that name.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_context, require_mutation_access
from app.core.models import SettingsSnapshot
from app.settings.service import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"])


def _service(request: Request) -> SettingsService:
    context = get_context(request)
    return SettingsService(
        context.settings,
        context.settings_repo,
        player=getattr(context, "player", None),
    )


@router.get("", response_model=SettingsSnapshot)
async def read_settings(request: Request) -> SettingsSnapshot:
    """Whitelisted values plus the keys this API is allowed to change."""

    return await _service(request).snapshot()


@router.put(
    "",
    response_model=SettingsSnapshot,
    dependencies=[Depends(require_mutation_access)],
)
async def update_settings(
    request: Request, payload: dict[str, Any]
) -> SettingsSnapshot:
    """Partial update of editable settings; unknown keys are rejected with 422."""

    return await _service(request).update(payload)
