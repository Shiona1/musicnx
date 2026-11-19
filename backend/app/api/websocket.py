"""WebSocket endpoint: snapshot on connect, filtered events afterwards."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.context import AppContext
from app.core.event_bus import Subscription
from app.core.timeutils import utc_now_iso
from app.security import hostname_from_authority, is_allowed_hostname, is_allowed_origin

router = APIRouter()

SHELF_LIMIT = 24
_active_clients: set[WebSocket] = set()


def active_websocket_count() -> int:
    """Live WS clients for the runtime metrics endpoint."""

    return len(_active_clients)


@dataclass
class ClientState:
    visible: bool = False


async def build_snapshot(context: AppContext) -> dict[str, Any]:
    player = await context.player.snapshot()
    queue = await context.player.queue_state()
    shelf, total = await context.tracks.list(limit=SHELF_LIMIT)
    return {
        "type": "snapshot",
        "ts": utc_now_iso(),
        "payload": {
            "server": {
                "version": context.version,
                "mode": context.settings.mode,
                "started_at": context.started_at_iso(),
                "uptime_s": round(context.uptime_s, 3),
            },
            "player": player.model_dump(),
            "queue": queue.model_dump(),
            "library": {"shelf": [track.model_dump() for track in shelf], "total": total},
        },
    }


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "ts": utc_now_iso(),
        "payload": {"code": code, "message": message},
    }


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    context: AppContext = websocket.app.state.ctx
    security = context.settings.security
    host = hostname_from_authority(websocket.headers.get("host"))
    origin = websocket.headers.get("origin")
    if security.enforce_host and not is_allowed_hostname(host, security):
        await websocket.close(code=1008)
        return
    if origin is not None and not is_allowed_origin(origin, security):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    _active_clients.add(websocket)
    subscription = context.bus.subscribe()
    client = ClientState()
    send_lock = asyncio.Lock()

    async def send(message: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(message)

    try:
        await send(await build_snapshot(context))
        sender = asyncio.create_task(
            _sender_loop(subscription, client, send), name="ws-sender"
        )
        receiver = asyncio.create_task(
            _receiver_loop(websocket, client, send, subscription), name="ws-receiver"
        )
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in done:
            exception = task.exception()
            if exception is not None and not isinstance(
                exception, WebSocketDisconnect | RuntimeError
            ):
                raise exception
    except WebSocketDisconnect:
        pass
    finally:
        _active_clients.discard(websocket)
        context.bus.unsubscribe(subscription)


async def _sender_loop(subscription: Subscription, client: ClientState, send) -> None:
    while True:
        event = await subscription.queue.get()
        if event["type"] == "player.position" and not client.visible:
            continue
        await send(event)


async def _receiver_loop(
    websocket: WebSocket,
    client: ClientState,
    send,
    subscription: Subscription,
) -> None:
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await send(_error("bad_message", "message must be valid JSON"))
            continue
        if not isinstance(message, dict):
            await send(_error("bad_message", "message must be a JSON object"))
            continue
        message_type = message.get("type")
        payload = message.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if message_type == "client.hello":
            client.visible = bool(payload.get("visible", False))
            subscription.accepts_position = client.visible
        elif message_type == "ping":
            await send({"type": "pong", "ts": utc_now_iso(), "payload": {}})
        else:
            await send(_error("bad_message", f"unknown message type: {message_type!r}"))
