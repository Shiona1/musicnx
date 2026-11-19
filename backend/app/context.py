"""Application context: one object holding all wired singletons."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.audio.base import AudioBackend
from app.audio.factory import create_audio_backend, create_source_resolver
from app.audio.router import MockAudioRouter
from app.auth import PinAttemptLimiter
from app.bluetooth.service import BluetoothService
from app.config import Settings, ensure_runtime_dirs
from app.core.event_bus import EventBus
from app.core.player_manager import PlayerManager
from app.core.queue_manager import QueueManager
from app.core.timeutils import to_iso, utc_now, utc_now_iso
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.repositories import (
    LibraryRepository,
    PlaybackStateRepository,
    PlayHistoryRepository,
    QueueRepository,
    SettingsRepository,
    TrackRepository,
)
from app.database.seed import seed_mock_catalog
from app.downloads.manager import create_download_manager
from app.downloads.mock import MockDownloadPipeline
from app.downloads.pipeline import DownloadPipeline
from app.downloads.repository import DownloadRepository
from app.downloads.resolver import DownloadResolver
from app.downloads.tags import TrackTags
from app.downloads.yandex_source import YandexDownloadSource
from app.integrations.yandex_music.auth import MockYandexAuthManager, YandexAuthManager
from app.integrations.yandex_music.base import YandexAdapter
from app.integrations.yandex_music.cdn import CdnRouter
from app.integrations.yandex_music.repository import YandexRepository
from app.integrations.yandex_music.service import create_yandex_adapter
from app.library.covers import CoverCache
from app.library.fallback import TrackFallbackService
from app.library.importer import ImportManager
from app.logging_setup import get_logger, setup_logging
from app.lyrics.service import LyricsService
from app.maintenance.backup import BackupService
from app.maintenance.service import MaintenanceService
from app.maintenance.timer import MaintenanceTimer
from app.network.service import NetworkService
from app.pin_store import PinStore
from app.services.metrics import SystemMetricsService
from app.services.power import PowerService
from app.settings.service import SettingsService
from app.sync.service import SyncService
from app.zones.manager import create_zone_manager

logger = get_logger("context")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class AppContext:
    settings: Settings
    started_at: datetime
    pin_limiter: PinAttemptLimiter
    pin_store: PinStore
    db: Database
    bus: EventBus
    tracks: TrackRepository
    queue_repo: QueueRepository
    state_repo: PlaybackStateRepository
    settings_repo: SettingsRepository
    history: PlayHistoryRepository
    library_repo: LibraryRepository
    covers: CoverCache
    importer: ImportManager
    queue: QueueManager
    audio: AudioBackend
    player: PlayerManager
    yandex: YandexAdapter
    yandex_adapter: YandexAdapter
    yandex_auth: YandexAuthManager | MockYandexAuthManager
    yandex_streaming: Any | None
    downloads: Any
    downloads_pipeline: Any
    lyrics: LyricsService
    sync: SyncService
    zones: Any
    bluetooth: BluetoothService
    backups: BackupService
    maintenance: MaintenanceService
    settings_service: SettingsService
    maintenance_timer: MaintenanceTimer
    router: MockAudioRouter
    network: NetworkService
    metrics: SystemMetricsService
    power: PowerService
    cdn: CdnRouter

    @property
    def version(self) -> str:
        return __version__

    @property
    def uptime_s(self) -> float:
        return max((utc_now() - self.started_at).total_seconds(), 0.0)

    async def start(self) -> None:
        await self.audio.start()
        await self.player.restore()
        self.player.start_ticker()
        await self.downloads.start()
        await self.sync.startup()
        await self.zones.start()
        await self.settings_service.startup()
        await self.maintenance_timer.start()
        await self.metrics.start()
        await self.library_repo.rebuild_search_index_if_needed()

    async def stop(self) -> None:
        await self.maintenance_timer.stop()
        await self.metrics.stop()
        await self.power.close()
        await self.sync.stop()
        await self.zones.shutdown()
        await self.bluetooth.close()
        if self.downloads is not None and hasattr(self.downloads, "shutdown"):
            await self.downloads.shutdown()
        await self.importer.shutdown()
        await self.player.shutdown()
        await self.audio.shutdown()
        if self.downloads_pipeline is not None and hasattr(
            self.downloads_pipeline, "close"
        ):
            await self.downloads_pipeline.close()
        await self.yandex_adapter.close()
        await self.yandex_auth.close()
        await self.db.close()

    async def describe_adapters(self) -> dict[str, Any]:
        describe = getattr(self.yandex, "describe", None)
        yandex = (
            await describe()
            if callable(describe)
            else {"mode": getattr(self.yandex, "mode", "unknown")}
        )
        router, bluetooth, network = await asyncio.gather(
            self._describe_router(),
            self.bluetooth.status(),
            self.network.status(),
        )
        return {
            "yandex": yandex,
            "router": router,
            "bluetooth": bluetooth,
            "network": network,
        }

    async def _describe_router(self) -> dict[str, Any]:
        """Report the real zone router, not the legacy placeholder object."""

        zones = getattr(self, "zones", None)
        if zones is None:
            return await self.router.describe()
        try:
            page = await zones.list()
        except Exception:  # pragma: no cover - defensive boundary
            return await self.router.describe()
        return {
            "backend": page.backend,
            "degraded": page.degraded,
            "outputs": [
                {
                    "id": zone.id,
                    "kind": zone.kind,
                    "name": zone.name,
                    "sink_name": zone.sink_name,
                }
                for zone in page.items
            ],
        }

    def storage_paths(self) -> dict[str, str]:
        return {
            "data": str(self.settings.paths.data_dir),
            "database": str(self.settings.paths.database),
            "library": str(self.settings.paths.library),
            "downloads": str(self.settings.paths.downloads),
            "cache": str(self.settings.paths.cache),
        }

    def started_at_iso(self) -> str:
        return to_iso(self.started_at)


async def build_context(settings: Settings) -> AppContext:
    """Wire the application. Raises on configuration or DB failures."""

    setup_logging(settings.logging)
    ensure_runtime_dirs(settings)

    db = Database(settings.paths.database)
    await db.connect()
    schema_version = await apply_migrations(db)
    logger.info(
        "database ready: %s (schema v%s, mode=%s)",
        settings.paths.database,
        schema_version,
        settings.mode,
    )

    if settings.mode == "mock" and settings.mock.seed_catalog:
        track_count = await seed_mock_catalog(db)
        logger.info("mock catalog seeded: %s tracks", track_count)

    bus = EventBus()
    tracks = TrackRepository(db)
    queue_repo = QueueRepository(db)
    state_repo = PlaybackStateRepository(db)
    settings_repo = SettingsRepository(db)
    history = PlayHistoryRepository(db)
    library_repo = LibraryRepository(db)
    queue = QueueManager(queue_repo, tracks)

    covers = CoverCache(
        settings.paths.cache / "covers",
        max_bytes=settings.library.cover_cache_mb * 1024 * 1024,
        sizes=settings.library.cover_sizes,
    )
    covers.ensure_dir()

    async def on_warning(code: str, message: str) -> None:
        await bus.publish("system.warning", {"code": code, "message": message})

    yandex_auth: YandexAuthManager | MockYandexAuthManager
    if settings.mode == "mock":
        yandex_auth = MockYandexAuthManager(settings.yandex, bus)
    else:
        yandex_auth = YandexAuthManager(settings.yandex, bus)
    yandex_adapter = create_yandex_adapter(
        settings, bus, db, tracks, yandex_auth
    )
    yandex_streaming = getattr(yandex_adapter, "streaming", None)
    cdn = CdnRouter()

    audio = create_audio_backend(settings, on_warning=on_warning)
    resolver = create_source_resolver(settings, tracks, yandex_adapter, cdn)
    fallback = TrackFallbackService(tracks, yandex_adapter)
    player = PlayerManager(
        settings=settings,
        bus=bus,
        queue=queue,
        tracks=tracks,
        state_repo=state_repo,
        settings_repo=settings_repo,
        history=history,
        backend=audio,
        resolver=resolver,
        fallback=fallback,
    )
    importer = ImportManager(
        settings=settings,
        bus=bus,
        library=library_repo,
        covers=covers,
    )

    async def download_metadata(track_id: int) -> TrackTags | None:
        track = await tracks.get(track_id)
        if track is None:
            return None
        return TrackTags(
            title=track.title,
            artists=tuple(artist.name for artist in track.artists),
            album=track.album.title if track.album else None,
            albumartist=track.artists[0].name if track.artists else None,
            year=track.album.year if track.album else None,
            genre=track.genre,
            label=track.label,
        )

    if settings.mode == "mock":
        downloads_pipeline = MockDownloadPipeline(settings, tracks)
    else:
        yandex_repository = YandexRepository(db, tracks)
        downloads_source = YandexDownloadSource(
            settings, yandex_auth, yandex_repository, cdn=cdn
        )
        downloads_pipeline = DownloadPipeline(
            settings,
            downloads_source,
            metadata_provider=download_metadata,
        )

    async def register_download(task, result):
        return await library_repo.register_downloaded_file(
            task.track_id,
            path=result.path,
            format=result.format,
            size_bytes=result.bytes_written,
        )

    async def verify_download(media_file_id: int, result) -> None:
        await library_repo.set_verified(
            media_file_id, result.checksum, utc_now_iso()
        )

    async def discard_download(task) -> None:
        await downloads_pipeline.discard_partial(task.id)

    downloads = create_download_manager(
        repository=DownloadRepository(db),
        pipeline=downloads_pipeline,
        settings=settings,
        bus=bus,
        tracks=tracks,
        resolver=DownloadResolver(db),
        register_callback=register_download,
        verify_callback=verify_download,
        discard_callback=discard_download,
    )
    lyrics = LyricsService(
        settings, db, tracks, yandex_adapter, yandex_auth, bus
    )
    sync = SyncService(
        settings, bus, db, yandex_adapter, YandexRepository(db, tracks), tracks
    )
    zones = create_zone_manager(settings, bus, db, player)
    bluetooth = BluetoothService(settings, bus)
    backups = BackupService(settings, db)
    maintenance = MaintenanceService(settings, db)
    settings_service = SettingsService(settings, settings_repo, player=player)
    maintenance_timer = MaintenanceTimer(settings_repo, maintenance)
    network = NetworkService(settings, bus)
    metrics = SystemMetricsService(settings)
    power = PowerService()

    return AppContext(
        settings=settings,
        started_at=utc_now(),
        pin_limiter=PinAttemptLimiter(
            settings.paths.data_dir / "secrets" / "pin_attempts.json"
        ),
        pin_store=PinStore(settings),
        db=db,
        bus=bus,
        tracks=tracks,
        queue_repo=queue_repo,
        state_repo=state_repo,
        settings_repo=settings_repo,
        history=history,
        library_repo=library_repo,
        covers=covers,
        importer=importer,
        queue=queue,
        audio=audio,
        player=player,
        yandex=yandex_adapter,
        yandex_adapter=yandex_adapter,
        yandex_auth=yandex_auth,
        yandex_streaming=yandex_streaming,
        downloads=downloads,
        downloads_pipeline=downloads_pipeline,
        lyrics=lyrics,
        sync=sync,
        zones=zones,
        bluetooth=bluetooth,
        backups=backups,
        maintenance=maintenance,
        settings_service=settings_service,
        maintenance_timer=maintenance_timer,
        router=MockAudioRouter(),
        network=network,
        metrics=metrics,
        power=power,
        cdn=cdn,
    )
