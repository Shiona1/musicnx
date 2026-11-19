"""System endpoints: health, status and storage."""

from __future__ import annotations

import socket

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from app.api.deps import get_context, require_mutation_access
from app.api.websocket import active_websocket_count
from app.errors import AppError, UnavailableError
from app.services.qr import qr_png_data_uri
from app.services.storage import storage_payload

router = APIRouter(prefix="/system", tags=["system"])


class PowerRequest(BaseModel):
    """A destructive action is accepted only with an explicit confirmation."""

    confirm: bool = False


class SystemShare(BaseModel):
    """Web UI address for other devices on the LAN, with a scannable QR."""

    url: str
    qr_data_uri: str | None = None
    #: True when even the best known address is loopback-only (phone cannot
    #: reach it); the UI says so instead of showing a useless QR silently.
    loopback: bool = False


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _lan_ip() -> str | None:
    """Best-effort LAN address without sending packets (UDP connect trick)."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect() only picks a route locally; nothing is transmitted.
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, object]:
    context = get_context(request)
    try:
        await context.db.scalar("SELECT 1")
        database_ok = True
    except Exception:
        database_ok = False
    if not database_ok:
        response.status_code = 503
    router_backend = "real"
    degraded = False
    zones = getattr(context, "zones", None)
    if zones is not None:
        try:
            page = await zones.list()
            router_backend = page.backend
            degraded = page.degraded
        except Exception:
            router_backend = "unknown"
    return {
        "status": "ok" if database_ok else "error",
        "version": context.version,
        "mode": context.settings.mode,
        "uptime_s": round(context.uptime_s, 3),
        "db": "ok" if database_ok else "error",
        "audio_router": {"backend": router_backend, "degraded": degraded},
    }


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    context = get_context(request)
    player = await context.player.snapshot()
    schema_version = await context.db.schema_version()
    audio = context.audio.describe()
    # The runtime flag follows the active zone (browser zone mutes mpv); the
    # static config value is only the initial default.
    audio["browser_zone"] = bool(
        getattr(context.player, "browser_zone", context.settings.audio.browser_zone)
    )
    return {
        "version": context.version,
        "mode": context.settings.mode,
        "started_at": context.started_at_iso(),
        "uptime_s": round(context.uptime_s, 3),
        "player": player.model_dump(),
        "audio": audio,
        "database": {
            "path": str(context.settings.paths.database.resolve()),
            "size_bytes": context.db.size_bytes(),
            "schema_version": schema_version,
        },
        "adapters": await context.describe_adapters(),
        "security": {
            "auth": "pin" if context.settings.security.pin else "localhost_only",
            "pin_configured": bool(context.settings.security.pin),
        },
    }


@router.get("/runtime")
async def runtime(request: Request) -> dict[str, object]:
    """Small, additive runtime counters for diagnostics/watchdog.

    No secrets and no per-request scanning: queue/downloads are already
    persisted, and the WS counter is in-process.
    """

    context = get_context(request)
    queue = await context.player.queue_state()
    downloads = await context.downloads.list()
    by_status: dict[str, int] = {}
    for task in downloads.items:
        by_status[task.status] = by_status.get(task.status, 0) + 1
    audio = context.audio.describe()
    return {
        "uptime_s": round(context.uptime_s, 3),
        "ws_clients": active_websocket_count(),
        "queue_depth": len(queue.items),
        "downloads": {"total": downloads.total, "by_status": by_status},
        "database": {"size_bytes": context.db.size_bytes()},
        "audio": {
            "ready": audio.get("ready"),
            "restarts": audio.get("restarts"),
            "error": audio.get("error"),
        },
        "security": {
            "auth": "trusted_lan" if not context.settings.security.pin else "legacy",
        },
    }


@router.get("/storage")
async def storage(request: Request) -> dict[str, object]:
    context = get_context(request)
    payload = await storage_payload(
        context.settings,
        context.settings.paths.database,
        context.db.size_bytes(),
    )
    return payload


@router.get("/metrics")
async def metrics(
    request: Request,
    minutes: int = Query(default=60, ge=1, le=1440),
) -> dict[str, object]:
    """Recent CPU/memory/temperature samples for the status page."""

    context = get_context(request)
    service = getattr(context, "metrics", None)
    if service is None:
        raise UnavailableError("system metrics are not configured")
    return await service.snapshot(minutes)


@router.get("/share", response_model=SystemShare)
async def share(request: Request) -> SystemShare:
    """The address this browser used, translated for other LAN devices.

    The Host header is what the current client actually reached, so a LAN
    hostname or IP is passed through unchanged. A loopback host is swapped
    for the LAN address when one exists; the ``loopback`` flag tells the UI
    the URL is still phone-unreachable instead of hiding that.
    """

    context = get_context(request)
    host = request.headers.get("host", "")
    hostname, _, port = host.partition(":")
    if hostname in _LOOPBACK_HOSTS:
        lan_ip = _lan_ip()
        if lan_ip:
            host = f"{lan_ip}:{port or context.settings.server.port}"
    hostname = host.partition(":")[0]
    url = f"http://{host}"
    return SystemShare(
        url=url,
        qr_data_uri=qr_png_data_uri(url),
        loopback=hostname in _LOOPBACK_HOSTS,
    )


@router.post(
    "/reboot",
    status_code=202,
    dependencies=[Depends(require_mutation_access)],
)
async def reboot(request: Request, payload: PowerRequest) -> dict[str, object]:
    """Schedule `systemctl reboot`; the HTTP answer returns first."""

    if not payload.confirm:
        raise AppError(
            "reboot requires explicit confirmation",
            code="confirmation_required",
            status_code=422,
        )
    return await _request_power(request, "reboot")


@router.post(
    "/shutdown",
    status_code=202,
    dependencies=[Depends(require_mutation_access)],
)
async def shutdown(request: Request, payload: PowerRequest) -> dict[str, object]:
    """Schedule `systemctl poweroff`; the HTTP answer returns first."""

    if not payload.confirm:
        raise AppError(
            "shutdown requires explicit confirmation",
            code="confirmation_required",
            status_code=422,
        )
    return await _request_power(request, "poweroff")


async def _request_power(request: Request, action: str) -> dict[str, object]:
    context = get_context(request)
    service = getattr(context, "power", None)
    if service is None:
        raise UnavailableError("power actions are not configured")
    await service.request(action)  # type: ignore[arg-type]
    return {"status": f"{action}_scheduled", "delay_s": 2}
