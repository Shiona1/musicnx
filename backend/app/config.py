"""Application settings loaded from YAML, environment and defaults.

Priority (highest first): explicit constructor kwargs, environment variables
(``MUSICNX_*``, nested with ``__``), ``.env``, YAML file, defaults.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class ServerSettings(BaseModel):
    """HTTP server bind settings."""

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class PathsSettings(BaseModel):
    """Runtime paths. All values may be absolute (recommended on the Pi)."""

    data_dir: Path = Path("var")
    database: Path = Path("var/musicnx.db")
    library: Path = Path("var/library")
    downloads: Path = Path("var/downloads")
    cache: Path = Path("var/cache")
    backups: Path = Path("var/backups")

    def directories(self) -> list[Path]:
        return [
            self.data_dir,
            self.database.parent,
            self.library,
            self.downloads,
            self.cache,
            self.backups,
        ]


def default_ipc_socket() -> Path:
    """Resolve the mpv IPC socket from the runtime dir instead of a fixed UID."""

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            try:
                runtime_dir = f"/run/user/{getuid()}"
            except OSError:  # pragma: no cover - depends on the platform
                runtime_dir = None
    if not runtime_dir:
        runtime_dir = tempfile.gettempdir()
    return Path(runtime_dir) / "musicnx" / "mpv.sock"


class PlayerSettings(BaseModel):
    """Player behaviour and persistence cadence."""

    default_volume: int = Field(default=70, ge=0, le=100)
    autoplay: bool = True
    position_persist_interval_s: float = Field(default=7.0, gt=0)
    position_event_hz: float = Field(default=2.0, ge=0.5, le=10.0)
    previous_restart_threshold_ms: int = Field(default=3000, ge=0)


class LoggingSettings(BaseModel):
    """Logging setup; ``file=None`` disables file output (used by tests)."""

    level: str = "INFO"
    file: Path | None = Path("var/log/musicnx.log")
    max_bytes: int = Field(default=5_242_880, ge=0)
    backup_count: int = Field(default=3, ge=0)

    @field_validator("level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("invalid log level")
        return normalized


class YandexSettings(BaseModel):
    """Yandex Music settings (P3)."""

    enabled: bool = False
    token_file: Path | None = Path("var/secrets/yandex_token.json")
    quality: Literal["flac", "aac", "mp3"] = "mp3"
    client_id: str | None = None
    client_secret: str | None = None
    device_name: str = "MusicNX"
    request_timeout_s: float = 15.0
    refresh_before_s: int = Field(default=120, ge=30)


class AudioSettings(BaseModel):
    """Audio backend selection (mpv is the only real backend)."""

    backend: Literal["auto", "mpv", "mock"] = "auto"
    mpv_path: str = "mpv"
    ipc_socket: Path = Field(default_factory=default_ipc_socket)
    start_timeout_s: float = Field(default=10.0, gt=0)
    restart_backoff_s: list[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0])
    max_restart_attempts: int = Field(default=5, ge=1)
    # P3 browser zone: mpv stays the clock but its local output is muted and
    # clients play /stream/{track_id} in an <audio> element.
    browser_zone: bool = False

    @field_validator("restart_backoff_s")
    @classmethod
    def _non_empty_backoff(cls, value: list[float]) -> list[float]:
        if not value or any(v <= 0 for v in value):
            raise ValueError("restart_backoff_s must contain positive delays")
        return value


class SyncSettings(BaseModel):
    """Catalog synchronization settings (P6)."""

    enabled: bool = False
    interval_s: int = Field(default=3600, ge=300)


class LyricsSettings(BaseModel):
    """Lyrics providers beyond local ``.lrc`` and Yandex Music."""

    # LRCLIB is a free community lyrics database; disabled in tests by
    # ``make_settings`` so no test ever reaches the network.
    lrclib_enabled: bool = True
    lrclib_timeout_s: float = Field(default=6.0, ge=1.0, le=30.0)


class DownloadsSettings(BaseModel):
    """P4 download queue and file pipeline."""

    enabled: bool = True
    # User preference for new downloads: auto | flac | aac | mp3. ``auto`` and
    # explicit choices both fall back when Yandex does not offer that family;
    # the task's actual format is always reported honestly.
    quality: Literal["auto", "flac", "aac", "mp3"] = "auto"
    # Ordered preference; files are never transcoded, only picked.
    quality_order: list[str] = Field(default_factory=lambda: ["flac", "aac", "mp3"])
    max_parallel: int = Field(default=1, ge=1, le=2)
    pause_min_s: float = Field(default=2.0, ge=0)
    pause_max_s: float = Field(default=5.0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    backoff_s: list[float] = Field(default_factory=lambda: [5.0, 30.0, 120.0])
    cover_size: int = 1000
    path_template: str = "{artists}/{album} ({year})/Disc {disc}/{track:02d} - {title}{ext}"


class LibrarySettings(BaseModel):
    """Local media library import and artwork cache."""

    # Empty means "use paths.library". Every configured root must already exist.
    import_roots: list[Path] = Field(default_factory=list)
    extensions: list[str] = Field(
        default_factory=lambda: [".mp3", ".flac", ".aac", ".m4a", ".wav"]
    )
    cover_cache_mb: int = Field(default=64, ge=1, le=4096)
    cover_sizes: list[int] = Field(default_factory=lambda: [300, 1000, 64])
    checksum_chunk_bytes: int = Field(default=1_048_576, ge=4096)
    # Browser uploads are streamed to disk and then imported; keep a hard cap so
    # a single request cannot fill the Pi's SD card.
    upload_max_bytes: int = Field(default=300 * 1024 * 1024, ge=1024 * 1024)
    # Total size of <import root>/uploads; 0 disables the quota.
    upload_quota_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=0)
    # Refuse uploads that would leave the filesystem below this reserve.
    upload_min_free_bytes: int = Field(default=512 * 1024 * 1024, ge=0)

    @field_validator("extensions")
    @classmethod
    def _normalize_extensions(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            suffix = value.strip().lower()
            if not suffix.startswith("."):
                suffix = f".{suffix}"
            if len(suffix) < 2:
                raise ValueError("invalid media extension")
            normalized.append(suffix)
        return sorted(set(normalized))

    @field_validator("cover_sizes")
    @classmethod
    def _validate_sizes(cls, values: list[int]) -> list[int]:
        if 300 not in values:
            raise ValueError("cover_sizes must include 300")
        for value in values:
            if value < 16 or value > 4096:
                raise ValueError("cover size out of range")
        return sorted(set(values))


class SecuritySettings(BaseModel):
    """LAN security: Host/Origin validation, PIN and optional CORS origins."""

    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1", "musicnx.local"]
    )
    allowed_origins: list[str] = Field(default_factory=list)
    enforce_host: bool = True
    # Mutating endpoints require this PIN when set; otherwise localhost only.
    # Keep it out of logs and API responses (redaction filter covers "pin").
    pin: str | None = None
    # Failed PIN attempts are limited per client IP in memory.
    pin_max_attempts: int = Field(default=5, ge=1)
    pin_lockout_s: float = Field(default=60.0, gt=0)
    # Optional explicit cookie signing key. Fallback: derived from the PIN.
    session_secret: str | None = None
    session_ttl_s: int = Field(default=604_800, ge=60)

    @field_validator("pin")
    @classmethod
    def _pin_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 4:
            raise ValueError("security.pin must be at least 4 characters")
        return value


class MockSettings(BaseModel):
    """Mock mode behaviour."""

    seed_catalog: bool = True


class _YamlSettingsSource(PydanticBaseSettingsSource):
    """Load a YAML mapping; a missing file is not an error."""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        loaded = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {self._path} must contain a YAML mapping")
        return loaded


class Settings(BaseSettings):
    """Root MusicNX configuration."""

    model_config = SettingsConfigDict(
        env_prefix="MUSICNX_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mode: Literal["mock", "real"] = "mock"
    server: ServerSettings = Field(default_factory=ServerSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    player: PlayerSettings = Field(default_factory=PlayerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    yandex: YandexSettings = Field(default_factory=YandexSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    lyrics: LyricsSettings = Field(default_factory=LyricsSettings)
    downloads: DownloadsSettings = Field(default_factory=DownloadsSettings)
    library: LibrarySettings = Field(default_factory=LibrarySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    mock: MockSettings = Field(default_factory=MockSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_path = Path(os.environ.get("MUSICNX_CONFIG", str(DEFAULT_CONFIG_PATH)))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSettingsSource(settings_cls, config_path),
            file_secret_settings,
        )


def ensure_runtime_dirs(settings: Settings) -> None:
    """Create writable runtime directories; raise a clear error on failure."""

    dirs = settings.paths.directories()
    if settings.logging.file is not None:
        dirs.append(settings.logging.file.parent)
    if settings.mode == "real" and settings.audio.backend in {"auto", "mpv"}:
        dirs.append(settings.audio.ipc_socket.parent)
    for directory in dirs:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(f"cannot create runtime directory {directory}: {exc}") from exc


def resolved_import_roots(settings: Settings) -> list[Path]:
    """Configured roots, defaulting to paths.library, resolved and de-duplicated."""

    roots = settings.library.import_roots or [settings.paths.library]
    resolved: list[Path] = []
    for root in roots:
        path = root.expanduser().resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved
