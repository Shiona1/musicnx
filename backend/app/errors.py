"""Application-level errors mapped to a stable REST error envelope."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base error rendered as ``{"error": {...}}`` with a stable code."""

    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.code.replace("_", " ")
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class QueueChangedError(ConflictError):
    code = "queue_changed"


class NoPlayableSourceError(ConflictError):
    code = "no_playable_source"


class UnavailableError(AppError):
    status_code = 503
    code = "unavailable"


class SecurityError(AppError):
    status_code = 403
    code = "origin_forbidden"


class AuthenticationError(AppError):
    """PIN is configured but the session cookie is missing or invalid."""

    status_code = 401
    code = "auth_required"


class PinRequiredError(AppError):
    """No PIN configured: mutating requests are accepted from localhost only."""

    status_code = 403
    code = "pin_required"


class PinNotConfiguredError(AppError):
    status_code = 409
    code = "pin_not_configured"


class InvalidPinError(AppError):
    status_code = 401
    code = "invalid_pin"


class TooManyAttemptsError(AppError):
    """Too many failed PIN attempts from one client; retry after the window."""

    status_code = 429
    code = "too_many_attempts"


class ConfigurationError(RuntimeError):
    """Invalid or unsupported runtime configuration (fails fast at startup)."""
