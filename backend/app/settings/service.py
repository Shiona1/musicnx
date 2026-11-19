"""Settings whitelist service — P8, contract in docs/API.md §12.

``GET /settings`` exposes only the keys this service owns; ``PUT /settings``
accepts a partial mapping from the whitelist, validates it, persists the value
in the ``settings`` table (JSON-encoded through
:class:`~app.database.repositories.SettingsRepository`) and applies it to the
live services. Configuration defaults from :class:`app.config.Settings` are the
fallback until a key is stored for the first time, so an untouched installation
keeps behaving exactly as ``config.yaml``/env says.

Editable keys (honest whitelist — only values that can really be applied):

``player.default_volume``
    Integer 0..100. Applied through ``PlayerManager.set_volume`` immediately on
    ``PUT`` (which also persists the session volume and broadcasts
    ``volume.changed``) and applied again from the stored value on startup, so
    the setting survives a restart.

``sync.enabled`` is reported read-only. :class:`~app.sync.service.SyncService`
owns the hourly scheduler and only exposes ``start_scheduler()`` plus ``stop()``
— and ``stop()`` is the shutdown hook: it closes the service and cancels an
active run, so it cannot be used as a live toggle without breaking manual sync.
There is also no startup hook that re-reads a stored value, so persisting it
would not survive a restart either. Reporting it as editable would be a fake
(AGENTS.md §12); the key stays visible (value from config) but ``PUT`` rejects
it with ``422 validation_error`` and points at ``config.yaml`` + restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import Settings
from app.core.models import SettingsSnapshot
from app.database.repositories import SettingsRepository
from app.errors import AppError, UnavailableError
from app.logging_setup import get_logger

logger = get_logger("settings.service")

DEFAULT_VOLUME_KEY = "player.default_volume"
SYNC_ENABLED_KEY = "sync.enabled"
DOWNLOAD_QUALITY_KEY = "downloads.quality"
#: Session volume written by ``PlayerManager.set_volume``; never exposed by GET.
SESSION_VOLUME_KEY = "player.volume"

VOLUME_MIN = 0
VOLUME_MAX = 100
DOWNLOAD_QUALITIES: tuple[str, ...] = ("auto", "flac", "aac", "mp3")

EDITABLE_KEYS: tuple[str, ...] = (DEFAULT_VOLUME_KEY, DOWNLOAD_QUALITY_KEY)
#: Keys that are visible in the snapshot but rejected by PUT, with the reason.
READ_ONLY_KEYS: dict[str, str] = {
    SYNC_ENABLED_KEY: (
        "sync.enabled is read-only: the sync scheduler has no safe live toggle; "
        "set sync.enabled in config.yaml and restart MusicNX"
    )
}
_KNOWN_KEYS: frozenset[str] = frozenset((*EDITABLE_KEYS, *READ_ONLY_KEYS))

#: Distinguishes a missing key from an explicit JSON ``null`` value.
_UNSET = object()


def _validation_error(message: str, details: dict[str, Any]) -> AppError:
    return AppError(message, code="validation_error", status_code=422, details=details)


def valid_volume(value: Any) -> bool:
    """True for a real integer 0..100 (``bool`` is an ``int`` subclass, reject it)."""

    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and VOLUME_MIN <= value <= VOLUME_MAX
    )


def valid_download_quality(value: Any) -> bool:
    return isinstance(value, str) and value in DOWNLOAD_QUALITIES


class SettingsService:
    """Whitelist validation, persistence and live application of settings."""

    def __init__(
        self,
        settings: Settings,
        repository: SettingsRepository,
        *,
        player: Any | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        # Duck-typed on purpose: tests use a fake, production uses PlayerManager.
        self._player = player

    # ------------------------------------------------------------------ read

    async def snapshot(self) -> SettingsSnapshot:
        """Current whitelisted values plus the keys PUT may change."""

        stored_default = await self._repository.get(DEFAULT_VOLUME_KEY)
        default_volume = (
            stored_default
            if valid_volume(stored_default)
            else int(self._settings.player.default_volume)
        )
        stored_quality = await self._repository.get(DOWNLOAD_QUALITY_KEY)
        quality = (
            stored_quality
            if valid_download_quality(stored_quality)
            else self._settings.downloads.quality
        )
        values: dict[str, Any] = {
            DEFAULT_VOLUME_KEY: default_volume,
            DOWNLOAD_QUALITY_KEY: quality,
            # The scheduler still reads the config value, so report exactly that.
            SYNC_ENABLED_KEY: bool(self._settings.sync.enabled),
        }
        return SettingsSnapshot(values=values, editable=list(EDITABLE_KEYS))

    # ------------------------------------------------------------------ write

    async def update(self, changes: Mapping[str, Any]) -> SettingsSnapshot:
        """Validate a partial mapping, apply it live and persist it.

        Nothing is written when validation or live application fails, so a
        rejected request never changes the running system or the database.
        """

        if not changes:
            raise _validation_error(
                "at least one setting must be provided",
                {"editable": list(EDITABLE_KEYS)},
            )

        unknown = sorted(key for key in changes if key not in _KNOWN_KEYS)
        if unknown:
            raise _validation_error(
                "unknown setting key",
                {"unknown": unknown[:20], "editable": list(EDITABLE_KEYS)},
            )

        read_only = sorted(key for key in changes if key in READ_ONLY_KEYS)
        if read_only:
            raise _validation_error(
                READ_ONLY_KEYS[read_only[0]],
                {"read_only": read_only, "editable": list(EDITABLE_KEYS)},
            )

        volume = changes.get(DEFAULT_VOLUME_KEY, _UNSET)
        if volume is not _UNSET:
            if not valid_volume(volume):
                raise _validation_error(
                    "player.default_volume must be an integer between 0 and 100",
                    {"key": DEFAULT_VOLUME_KEY},
                )
            # Apply before persisting: if the player cannot apply it, the
            # database must keep the previous setting.
            await self._apply_volume(volume)
            await self._repository.set(DEFAULT_VOLUME_KEY, volume)
            logger.info("settings: %s applied and stored: %s", DEFAULT_VOLUME_KEY, volume)

        quality = changes.get(DOWNLOAD_QUALITY_KEY, _UNSET)
        if quality is not _UNSET:
            if not valid_download_quality(quality):
                raise _validation_error(
                    "downloads.quality must be auto, flac, aac or mp3",
                    {"key": DOWNLOAD_QUALITY_KEY, "allowed": list(DOWNLOAD_QUALITIES)},
                )
            # Mutating the process settings object is what the download source
            # reads at resolve time; persist only after validation succeeded.
            self._settings.downloads.quality = quality
            await self._repository.set(DOWNLOAD_QUALITY_KEY, quality)
            logger.info("settings: %s applied and stored: %s", DOWNLOAD_QUALITY_KEY, quality)

        return await self.snapshot()

    # ---------------------------------------------------------------- startup

    async def startup(self) -> None:
        """Apply the stored default volume after the player restored its state.

        Only an explicitly stored ``player.default_volume`` overrides the
        last-session volume; without it a fresh or upgraded install keeps the
        existing restore behaviour (session volume or config default).
        """

        stored_quality = await self._repository.get(DOWNLOAD_QUALITY_KEY)
        if valid_download_quality(stored_quality):
            self._settings.downloads.quality = stored_quality
            logger.info(
                "settings: applied stored %s=%s on startup",
                DOWNLOAD_QUALITY_KEY,
                stored_quality,
            )
        stored = await self._repository.get(DEFAULT_VOLUME_KEY)
        if not valid_volume(stored):
            return
        set_volume = getattr(self._player, "set_volume", None)
        if not callable(set_volume):
            logger.warning(
                "settings: stored %s=%s cannot be applied: player is unavailable",
                DEFAULT_VOLUME_KEY,
                stored,
            )
            return
        await set_volume(stored)
        logger.info("settings: applied stored %s=%s on startup", DEFAULT_VOLUME_KEY, stored)

    # ---------------------------------------------------------------- helpers

    async def _apply_volume(self, volume: int) -> None:
        set_volume = getattr(self._player, "set_volume", None)
        if not callable(set_volume):
            raise UnavailableError(
                "player volume control is unavailable",
                details={"key": DEFAULT_VOLUME_KEY},
            )
        await set_volume(volume)


__all__ = [
    "DEFAULT_VOLUME_KEY",
    "DOWNLOAD_QUALITIES",
    "DOWNLOAD_QUALITY_KEY",
    "EDITABLE_KEYS",
    "READ_ONLY_KEYS",
    "SESSION_VOLUME_KEY",
    "SYNC_ENABLED_KEY",
    "SettingsService",
    "valid_download_quality",
    "valid_volume",
]
