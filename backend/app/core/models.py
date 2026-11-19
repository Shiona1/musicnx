"""Core domain models shared by API, player and repositories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PlayerStatus = Literal["stopped", "playing", "paused", "buffering", "error"]
Availability = Literal["offline", "online", "metadata_only", "missing", "unavailable"]
RepeatMode = Literal["off", "one", "all"]


class ArtistRef(BaseModel):
    id: int
    name: str


class AlbumRef(BaseModel):
    id: int
    title: str
    year: int | None = None
    cover_path: str | None = None


class Track(BaseModel):
    """One composition, regardless of how many files or sources it has."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    artists: list[ArtistRef] = Field(default_factory=list)
    album: AlbumRef | None = None
    duration_ms: int | None = None
    genre: str | None = None
    label: str | None = None
    availability: Availability = "metadata_only"
    has_local_file: bool = False
    has_online_source: bool = False
    file_missing: bool = False
    source_unavailable: bool = False
    cover_url: str | None = None
    # P3: online catalog provenance (local tracks leave these unset).
    source: str | None = None
    external_id: str | None = None
    preview: bool = False
    liked: bool | None = None


class AlbumSummary(BaseModel):
    id: int
    title: str
    year: int | None = None
    artist: ArtistRef | None = None
    cover_url: str | None = None
    track_count: int = 0


class ArtistSummary(BaseModel):
    id: int
    name: str
    cover_url: str | None = None
    track_count: int = 0
    album_count: int = 0


class AlbumPage(BaseModel):
    items: list[AlbumSummary]
    total: int
    limit: int
    offset: int


class ArtistPage(BaseModel):
    items: list[ArtistSummary]
    total: int
    limit: int
    offset: int


class AlbumDetail(BaseModel):
    """One local album with its track list."""

    album: AlbumSummary
    tracks: list[Track] = Field(default_factory=list)


class ArtistDetail(BaseModel):
    """One local artist with their track list."""

    artist: ArtistSummary
    tracks: list[Track] = Field(default_factory=list)


class QueueItem(BaseModel):
    id: int
    position: int
    source_playlist_id: int | None = None
    track: Track


class QueueState(BaseModel):
    items: list[QueueItem] = Field(default_factory=list)
    current_item_id: int | None = None
    current_index: int | None = None
    total: int = 0
    total_duration_ms: int = 0


class PlayerState(BaseModel):
    status: PlayerStatus = "stopped"
    track: Track | None = None
    position_ms: int = 0
    duration_ms: int = 0
    volume: int = 70
    # Transport modes. Shuffle picks a random playable item on advance;
    # repeat "one" replays the current track on EOF, "all" wraps the queue.
    shuffle: bool = False
    repeat: RepeatMode = "off"
    zone_id: str = "local"
    queue_index: int | None = None
    queue_item_id: int | None = None
    can_play: bool = False
    can_pause: bool = False
    can_next: bool = False
    can_previous: bool = False
    # Last playback failure, cleared on the next successful load. The code is a
    # stable machine token (UI localises it); detail is a short technical hint.
    error_code: str | None = None
    error_detail: str | None = None
    updated_at: str = ""


class ServerInfo(BaseModel):
    version: str
    mode: str
    started_at: str
    uptime_s: float


class LibraryPage(BaseModel):
    items: list[Track]
    total: int
    limit: int
    offset: int


class HistoryEntry(BaseModel):
    id: int
    track: Track
    played_at: str
    zone_id: str | None = None


class HistoryPage(BaseModel):
    items: list[HistoryEntry] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class SearchResults(BaseModel):
    tracks: LibraryPage
    albums: AlbumPage
    artists: ArtistPage


class YandexAuthStatus(BaseModel):
    authenticated: bool
    login: str | None = None
    expires_at: str | None = None
    source: Literal["device", "manual"] | None = None


class YandexDeviceStart(BaseModel):
    flow_id: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int
    qr_data_uri: str | None = None


class YandexDeviceStatus(BaseModel):
    state: Literal["pending", "authorized", "expired", "denied", "error"]
    flow_id: str
    login: str | None = None
    expires_at: str | None = None
    error: str | None = None


class YandexPlaylist(BaseModel):
    id: str
    kind: int
    title: str
    track_count: int
    cover_url: str | None = None
    owner: str | None = None


class YandexPlaylistPage(BaseModel):
    items: list[YandexPlaylist] = Field(default_factory=list)
    total: int = 0


class YandexAlbumDetail(BaseModel):
    album: AlbumSummary
    tracks: list[Track] = Field(default_factory=list)


class YandexArtistDetail(BaseModel):
    artist: ArtistSummary
    tracks: list[Track] = Field(default_factory=list)


class YandexSearchResults(BaseModel):
    tracks: LibraryPage
    albums: AlbumPage
    artists: ArtistPage
    playlists: YandexPlaylistPage


class WaveState(BaseModel):
    station: str = "user:onyourwave"
    batch_id: str | None = None
    session_id: str | None = None
    tracks: list[Track] = Field(default_factory=list)
    current_index: int = 0
    updated_at: str = ""


DownloadStatus = Literal[
    "queued", "downloading", "paused", "completed", "failed", "cancelled"
]


class DownloadTask(BaseModel):
    id: int
    track_id: int
    title: str | None = None
    status: DownloadStatus
    priority: int = 0
    attempts: int = 0
    bytes_done: int = 0
    bytes_total: int | None = None
    target_path: str | None = None
    quality: str | None = None
    format: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class DownloadTaskPage(BaseModel):
    items: list[DownloadTask] = Field(default_factory=list)
    total: int = 0


class DownloadRequest(BaseModel):
    track_id: int | None = None
    album_id: int | None = None
    playlist_id: str | None = None
    priority: int = 0


class LyricLine(BaseModel):
    time_ms: int
    text: str


class LyricsDTO(BaseModel):
    track_id: int
    available: bool
    format: Literal["lrc", "plain", "none"] = "none"
    language: str = "und"
    source: str | None = None
    offset_ms: int = 0
    lines: list[LyricLine] = Field(default_factory=list)
    plain: str | None = None


SyncStatusValue = Literal["running", "success", "partial", "failed", "cancelled"]


class SyncRun(BaseModel):
    id: int
    started_at: str
    finished_at: str | None = None
    status: SyncStatusValue
    playlists_added: int = 0
    playlists_updated: int = 0
    playlists_removed: int = 0
    tracks_added: int = 0
    tracks_updated: int = 0
    tracks_removed: int = 0
    error: str | None = None


class SyncStatus(BaseModel):
    running: bool = False
    last: SyncRun | None = None
    next_run_at: str | None = None
    interval_s: int = 3600
    progress: str | None = None


class SyncHistory(BaseModel):
    items: list[SyncRun] = Field(default_factory=list)
    total: int = 0


class AudioZone(BaseModel):
    id: str
    kind: Literal["local_sink", "browser", "bt"]
    name: str
    sink_name: str | None = None
    volume: int = 100
    offset_ms: int = 0
    active: bool = False
    available: bool = True


class ZonePage(BaseModel):
    items: list[AudioZone] = Field(default_factory=list)
    total: int = 0
    backend: str = "mock"
    degraded: bool = False


class BluetoothDevice(BaseModel):
    address: str
    name: str | None = None
    paired: bool = False
    connected: bool = False


class BluetoothDevicePage(BaseModel):
    items: list[BluetoothDevice] = Field(default_factory=list)
    total: int = 0
    backend: str = "mock"


class BackupInfo(BaseModel):
    name: str
    size_bytes: int
    created_at: str
    verified: bool | None = None


class BackupPage(BaseModel):
    items: list[BackupInfo] = Field(default_factory=list)
    total: int = 0
    directory: str


class IntegrityResult(BaseModel):
    ok: bool
    database: str
    quick_check: str
    schema_version: int | None = None


class MaintenanceResult(BaseModel):
    ok: bool = True
    removed_files: int = 0
    freed_bytes: int = 0
    wal_checkpointed: bool = False
    integrity: IntegrityResult | None = None
    message: str | None = None


class SettingsSnapshot(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    editable: list[str] = Field(default_factory=list)


class NetworkStatus(BaseModel):
    backend: str = "mock"
    connected: bool = False
    interface: str | None = None
    ssid: str | None = None
    ip: str | None = None
    signal: int | None = None
    internet: bool = False
    message: str | None = None


class WifiNetwork(BaseModel):
    ssid: str
    signal: int = 0
    security: str | None = None
    connected: bool = False


class WifiScanPage(BaseModel):
    items: list[WifiNetwork] = Field(default_factory=list)
    total: int = 0
    backend: str = "mock"


@dataclass(frozen=True)
class LocalMediaMetadata:
    """Tags and path of one local media file (produced off the event loop)."""

    path: Path
    relative_path: str
    format: str
    title: str
    artists: list[str]
    album: str | None
    albumartist: str | None
    year: int | None
    genre: str | None
    track_number: int | None
    disc_number: int | None
    label: str | None
    duration_ms: int | None
    size_bytes: int
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None


@dataclass(frozen=True)
class ImportOutcome:
    track_id: int
    media_file_id: int
    status: Literal["created", "updated", "skipped"]
    album_id: int | None = None
