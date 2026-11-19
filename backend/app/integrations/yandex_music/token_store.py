"""Yandex OAuth token persistence.

The token file lives outside the web root and is readable by the service user
only (``0600``). Writes are atomic (temp file in the same directory followed by
``os.replace``), so a crash can never leave a half-written token file behind.

Token values are secrets: they are never logged, never included in exception
messages and never rendered by ``repr``.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.logging_setup import get_logger

logger = get_logger("yandex.token_store")

TokenSource = Literal["device", "manual"]

_TOKEN_SOURCES = ("device", "manual")
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


@dataclass(frozen=True)
class TokenData:
    """One stored Yandex OAuth grant.

    ``obtained_at`` is a UNIX epoch timestamp used to derive ``expires_at``;
    manual tokens have no ``expires_in`` and therefore never expire proactively.
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None
    obtained_at: float = field(default_factory=time.time)
    token_type: str | None = None
    login: str | None = None
    source: TokenSource = "manual"

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token:
            raise ValueError("access_token must be a non-empty string")
        if self.source not in _TOKEN_SOURCES:
            raise ValueError("source must be 'device' or 'manual'")
        if self.expires_in is not None and self.expires_in < 0:
            raise ValueError("expires_in must not be negative")

    @property
    def expires_at(self) -> float | None:
        """Epoch seconds when the access token expires, if known."""

        if self.expires_in is None:
            return None
        return self.obtained_at + float(self.expires_in)

    def is_expired(self, *, now: float | None = None) -> bool:
        expires_at = self.expires_at
        if expires_at is None:
            return False
        return expires_at <= (time.time() if now is None else now)

    def to_payload(self) -> dict[str, Any]:
        """JSON-serializable representation persisted to disk."""

        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": self.expires_in,
            "obtained_at": self.obtained_at,
            "token_type": self.token_type,
            "login": self.login,
            "source": self.source,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TokenData:
        """Build a token from decoded JSON; raises ``ValueError`` when invalid."""

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("access_token is missing")

        obtained_at = payload.get("obtained_at", 0.0)
        if isinstance(obtained_at, bool) or not isinstance(obtained_at, int | float):
            raise ValueError("obtained_at must be an epoch timestamp")

        expires_in = payload.get("expires_in")
        if expires_in is not None and (
            isinstance(expires_in, bool) or not isinstance(expires_in, int)
        ):
            raise ValueError("expires_in must be an integer or null")

        source = payload.get("source", "manual")
        if source not in _TOKEN_SOURCES:
            raise ValueError("source must be 'device' or 'manual'")

        return cls(
            access_token=access_token,
            refresh_token=_optional_str(payload.get("refresh_token")),
            expires_in=expires_in,
            obtained_at=float(obtained_at),
            token_type=_optional_str(payload.get("token_type")),
            login=_optional_str(payload.get("login")),
            source=source,
        )

    def __repr__(self) -> str:
        """Never render secrets, even in debug traces."""

        refresh = "***" if self.refresh_token else None
        return (
            f"TokenData(login={self.login!r}, source={self.source!r}, "
            f"expires_at={self.expires_at!r}, access_token='***', "
            f"refresh_token={refresh!r})"
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("token field must be a string or null")
    return value or None


class TokenStore:
    """Reads and writes one ``TokenData`` JSON document at ``path``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> TokenData | None:
        """Return the stored token, or ``None`` when absent or unreadable.

        A corrupted file is never deleted (an operator may want to inspect it)
        and its content is never logged.
        """

        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("cannot read Yandex token file %s (%s)", self._path, type(exc).__name__)
            return None

        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("token file must contain a JSON object")
            return TokenData.from_payload(payload)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "ignoring corrupted Yandex token file %s (%s)",
                self._path,
                type(exc).__name__,
            )
            return None

    def save(self, token: TokenData) -> None:
        """Atomically persist ``token`` with ``0600`` permissions.

        Parent directories are created as needed. The temp file lives in the
        destination directory so ``os.replace`` stays atomic across filesystems.
        """

        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(token.to_payload(), ensure_ascii=False, indent=2) + "\n"

        handle, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _restrict_permissions(tmp_path)
            os.replace(tmp_path, self._path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        _restrict_permissions(self._path)

    def clear(self) -> None:
        """Remove the token file; a missing file is not an error."""

        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        logger.info("Yandex token removed from %s", self._path)


def _restrict_permissions(path: Path) -> None:
    """Best-effort ``0600``; Windows only maps the read-only bit and must not fail."""

    try:
        path.chmod(_FILE_MODE)
    except OSError:
        logger.warning("cannot restrict permissions of %s", path)
