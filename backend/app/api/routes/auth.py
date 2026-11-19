"""PIN authentication endpoints (mini-PIN for P2 mutating operations)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import get_context
from app.auth import (
    COOKIE_NAME,
    client_is_loopback,
    issue_session,
    validate_session,
)
from app.errors import InvalidPinError, PinNotConfiguredError, TooManyAttemptsError
from app.pin_store import PinStore

router = APIRouter(prefix="/auth", tags=["auth"])


class PinRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=128)


class PinChangeRequest(BaseModel):
    current_pin: str = Field(min_length=1, max_length=128)
    new_pin: str = Field(min_length=4, max_length=128)


def _store(context: object) -> PinStore:
    store = getattr(context, "pin_store", None)
    if isinstance(store, PinStore):
        return store
    return PinStore(context.settings)


@router.get("/status")
async def auth_status(request: Request) -> dict[str, bool]:
    context = get_context(request)
    settings = context.settings.security
    store = _store(context)
    token = request.cookies.get(COOKIE_NAME)
    return {
        "pin_configured": store.configured,
        "authenticated": store.configured and validate_session(token, settings),
        "localhost": client_is_loopback(request),
    }


@router.post("/pin")
async def login(request: Request, response: Response, payload: PinRequest) -> dict[str, bool]:
    context = get_context(request)
    settings = context.settings.security
    store = _store(context)
    if not store.configured:
        raise PinNotConfiguredError("PIN is not configured; mutating endpoints are localhost-only")
    client_key = request.client.host if request.client is not None else "unknown"
    limiter = context.pin_limiter
    if limiter.is_blocked(client_key, settings):
        raise TooManyAttemptsError(
            "too many failed PIN attempts",
            details={"retry_after_s": settings.pin_lockout_s},
        )
    if not store.verify(payload.pin):
        limiter.register_failure(client_key, settings)
        raise InvalidPinError("invalid PIN")
    limiter.reset(client_key)
    token = issue_session(settings)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_s,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"authenticated": True, "pin_configured": True}


@router.post("/pin/change")
async def change_pin(
    request: Request, response: Response, payload: PinChangeRequest
) -> dict[str, bool]:
    """Owner-initiated PIN rotation; current PIN is verified, never echoed."""

    context = get_context(request)
    settings = context.settings.security
    store = _store(context)
    if not store.configured:
        raise PinNotConfiguredError("PIN is not configured")
    client_key = request.client.host if request.client is not None else "unknown"
    limiter = context.pin_limiter
    if limiter.is_blocked(client_key, settings):
        raise TooManyAttemptsError(
            "too many failed PIN attempts",
            details={"retry_after_s": settings.pin_lockout_s},
        )
    if not store.verify(payload.current_pin):
        limiter.register_failure(client_key, settings)
        raise InvalidPinError("invalid PIN")
    await store.set_pin(payload.new_pin)
    # Re-key the live HMAC sessions immediately; config.yaml remains bootstrap.
    settings.pin = payload.new_pin
    limiter.reset(client_key)
    response.set_cookie(
        COOKIE_NAME,
        issue_session(settings),
        max_age=settings.session_ttl_s,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"changed": True, "pin_configured": True}


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}
