"""PIN sessions and localhost detection for mutating endpoints.

Read-only player/queue/library endpoints stay open in the LAN MVP (ADR-021).
State-changing endpoints require a valid PIN session, or localhost when no PIN
is configured. Tokens are HMAC-signed, HttpOnly and SameSite=Strict.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from ipaddress import ip_address
from pathlib import Path

from starlette.requests import Request

from app.config import SecuritySettings

COOKIE_NAME = "musicnx_session"
_TOKEN_VERSION = "v1"


class PinAttemptLimiter:
    """In-memory sliding-window limiter for failed PIN attempts per client IP.

    It deliberately never logs or stores the submitted PIN: only timestamps are
    kept and a successful login clears the client's history.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._failures: dict[str, list[float]] = {}
        self._path = path
        if path is not None:
            self._load()

    @staticmethod
    def _client_key(client_key: str) -> str:
        return hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:32]

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._failures = {
                str(key): [float(stamp) for stamp in values]
                for key, values in raw.items()
                if isinstance(values, list)
            }
        except (FileNotFoundError, OSError, ValueError, TypeError):
            self._failures = {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._failures, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)

    def _recent(
        self, client_key: str, window_s: float, now: float
    ) -> list[float]:
        key = self._client_key(client_key)
        cutoff = now - window_s
        attempts = [
            stamp for stamp in self._failures.get(key, ()) if stamp > cutoff
        ]
        if attempts:
            self._failures[key] = attempts
        else:
            self._failures.pop(key, None)
        return attempts

    def is_blocked(
        self,
        client_key: str,
        settings: SecuritySettings,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        attempts = self._recent(client_key, settings.pin_lockout_s, current)
        return len(attempts) >= settings.pin_max_attempts

    def register_failure(
        self,
        client_key: str,
        settings: SecuritySettings,
        *,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else now
        self._recent(client_key, settings.pin_lockout_s, current)
        key = self._client_key(client_key)
        self._failures.setdefault(key, []).append(current)
        self._save()

    def reset(self, client_key: str) -> None:
        self._failures.pop(self._client_key(client_key), None)
        self._save()


def _signing_key(settings: SecuritySettings) -> bytes:
    secret = settings.session_secret or f"musicnx-pin:{settings.pin or ''}"
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def issue_session(settings: SecuritySettings, *, now: float | None = None) -> str:
    """Create a signed session token. Requires a configured PIN."""

    if not settings.pin:
        raise ValueError("cannot issue a session without a PIN")
    issued = int(now if now is not None else time.time())
    expires = issued + settings.session_ttl_s
    payload = f"{_TOKEN_VERSION}:{expires}:{secrets.token_hex(8)}"
    signature = hmac.new(_signing_key(settings), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{_b64encode(payload.encode('ascii'))}.{_b64encode(signature)}"


def validate_session(
    token: str | None, settings: SecuritySettings, *, now: float | None = None
) -> bool:
    if not token or not settings.pin:
        return False
    try:
        payload_part, signature_part = token.split(".", 1)
        payload = _b64decode(payload_part).decode("ascii")
        signature = _b64decode(signature_part)
    except (ValueError, UnicodeDecodeError):
        return False

    expected = hmac.new(_signing_key(settings), payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return False

    parts = payload.split(":", 2)
    if len(parts) != 3:
        return False
    version, expires_raw, _ = parts
    if version != _TOKEN_VERSION:
        return False
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    return expires > int(now if now is not None else time.time())


def verify_pin(settings: SecuritySettings, candidate: str | None) -> bool:
    if not settings.pin or candidate is None:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), settings.pin.encode("utf-8"))


def _is_loopback_ip(value: str | None) -> bool:
    if not value:
        return False
    if value in {"localhost", "testclient"}:
        return True
    try:
        address = ip_address(value)
    except ValueError:
        return False
    return address.is_loopback


def client_is_loopback(request: Request) -> bool:
    client = request.client
    return _is_loopback_ip(client.host if client is not None else None)
