"""Persistent PIN store: scrypt hash in ``var/secrets/pin.json`` (0600).

``security.pin`` in config.yaml is the bootstrap/fallback. Once the owner changes
the PIN through the API, only the scrypt record is consulted; the plaintext PIN
is never stored, logged or returned. Session cookies are still signed from the
in-memory ``SecuritySettings.pin`` value, which the change endpoint updates for
the current process (old cookies expire on TTL/restart).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings


class PinStoreError(RuntimeError):
    """Raised for an invalid PIN record/change request."""


@dataclass(frozen=True)
class _Record:
    salt: bytes
    digest: bytes
    n: int
    r: int
    p: int


class PinStore:
    def __init__(self, settings: Settings, *, path: Path | None = None) -> None:
        self._settings = settings
        self._path = path or (settings.paths.data_dir / "secrets" / "pin.json")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def configured(self) -> bool:
        return self._path.is_file() or bool(self._settings.security.pin)

    def _load(self) -> _Record | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return _Record(
                salt=base64.b64decode(raw["salt"]),
                digest=base64.b64decode(raw["hash"]),
                n=int(raw["n"]),
                r=int(raw["r"]),
                p=int(raw["p"]),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PinStoreError("PIN store is unreadable") from exc

    def verify(self, candidate: str | None) -> bool:
        if candidate is None:
            return False
        record = self._load()
        if record is None:
            stored = self._settings.security.pin
            if not stored:
                return False
            return hmac.compare_digest(
                candidate.encode("utf-8"), stored.encode("utf-8")
            )
        digest = hashlib.scrypt(
            candidate.encode("utf-8"),
            salt=record.salt,
            n=record.n,
            r=record.r,
            p=record.p,
            dklen=len(record.digest),
        )
        return hmac.compare_digest(digest, record.digest)

    async def set_pin(self, new_pin: str) -> None:
        await asyncio.to_thread(self._set_sync, new_pin)

    def _set_sync(self, new_pin: str) -> None:
        if not 4 <= len(new_pin) <= 128:
            raise PinStoreError("PIN must be 4..128 characters")
        salt = os.urandom(16)
        n, r, p = 2**14, 8, 1
        digest = hashlib.scrypt(
            new_pin.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32
        )
        payload = {
            "version": 1,
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": base64.b64encode(digest).decode("ascii"),
            "n": n,
            "r": r,
            "p": p,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)
