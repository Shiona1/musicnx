"""ZoneManager: the single writer of the active zone and per-zone settings.

Switching zones moves the mpv stream on the PipeWire side; it never stops or
pauses playback. The manager does not own the player's state machine — at most it
aligns the player's master volume with the newly active zone.

Everything is persisted before the ``zones.updated`` event is published, so any
client that refetches ``GET /zones`` sees the new state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import AudioZone, BluetoothDevice, ZonePage
from app.database.connection import Database
from app.errors import AppError, NotFoundError, UnavailableError
from app.logging_setup import get_logger
from app.zones.repository import (
    DEFAULT_ZONE_ID,
    ZONE_OFFSET_MAX_MS,
    ZONE_OFFSET_MIN_MS,
    ZoneRepository,
)
from app.zones.router import (
    CommandRunner,
    PipeWireRouter,
    SinkInfo,
    ZoneRouter,
    create_zone_router,
)

logger = get_logger("zones.manager")

BT_ZONE_ID = "bt"
#: PipeWire's A2DP sink naming convention (``bluez_output.AA_BB_..._FF.1``).
BT_SINK_PREFIX = "bluez_output."
WATCHDOG_INTERVAL_S = 15.0


class ZoneManager:
    """Owns zone activation, per-zone volume/offset and the zone snapshot."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        db: Database,
        router: ZoneRouter,
        player: Any | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._router = router
        self._player = player
        self._repo = ZoneRepository(db)
        self._lock = asyncio.Lock()
        self._ready = False
        self._watchdog: asyncio.Task[None] | None = None
        self._sink_warning_sent = False

    # ----------------------------------------------------------------- lifecycle

    async def ensure_ready(self) -> None:
        """Seed defaults once; safe to call from every request path."""

        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await self._repo.ensure_defaults()
            active_id = await self._repo.active_zone_id()
            if active_id is None or await self._repo.get(active_id) is None:
                # A stale setting (e.g. restored backup) must not leave every
                # zone inactive; fall back to the always-present local output.
                await self._repo.mark_active(DEFAULT_ZONE_ID)
            self._ready = True

    async def start(self) -> ZonePage:
        """Startup hook: seed rows, discover sinks and restore the active zone."""

        await self.ensure_ready()
        page = await self.refresh()
        active = next((zone for zone in page.items if zone.active), None)
        if active is not None:
            # The browser zone persists across restarts: mpv must stay muted so
            # a client that reconnects does not hear a doubled track on the Pi.
            await self._sync_player_zone(active)
        if self._settings.mode == "real" and page.degraded:
            logger.error(
                "audio router degraded to mock in real mode; install pactl/wpctl"
            )
            await self._bus.publish(
                "system.warning",
                {
                    "code": "audio_router_mock",
                    "message": "audio route is mock in real mode; install pactl",
                },
            )
        if self._watchdog is None and isinstance(self._router, PipeWireRouter):
            self._watchdog = asyncio.create_task(
                self._watchdog_loop(), name="audio-sink-watchdog"
            )
        return page

    async def refresh(self, *, publish: bool = True) -> ZonePage:
        """Reconcile stored sink names with the sinks PipeWire reports now."""

        await self.ensure_ready()
        sinks = await self._router.list_sinks()
        sink_names = {sink.name for sink in sinks}
        preferred = self._preferred_sink(sinks)
        zones = await self._repo.list()
        for zone in zones:
            if zone.kind == "bt":
                # The BT placeholder loses its sink when the device disappears.
                # Only the real PipeWire list is authoritative; mock/test routers
                # keep virtual sinks and must not erase a just-registered device.
                clears = isinstance(self._router, PipeWireRouter)
                if zone.sink_name and zone.sink_name not in sink_names and clears:
                    await self._repo.set_sink_name(zone.id, None)
                elif not zone.sink_name:
                    # A device may be connected outside the app (bluetoothctl,
                    # desktop, auto-reconnect): adopt its A2DP sink so the zone
                    # becomes usable without a second "connect" click.
                    candidate = next(
                        (
                            sink.name
                            for sink in sinks
                            if sink.name.startswith(BT_SINK_PREFIX)
                        ),
                        None,
                    )
                    if candidate:
                        await self._repo.set_sink_name(zone.id, candidate)
            elif (
                preferred is not None
                and zone.id == DEFAULT_ZONE_ID
                and zone.sink_name not in sink_names
            ):
                await self._repo.set_sink_name(zone.id, preferred)
        page = await self.list()
        if publish:
            await self._publish(page)
        return page

    async def shutdown(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None
        close = getattr(self._router, "close", None)
        if callable(close):
            await close()

    async def _watchdog_loop(self) -> None:
        """Detect a disappeared sink and fall back to the local output.

        mpv's own supervisor handles a dead mpv process. This task covers the
        other half: PipeWire is alive, mpv is alive, but the active BT/USB sink
        vanished (device off, WirePlumber restart). We never restart mpv here.
        """

        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            try:
                await self._check_active_sink()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("audio sink watchdog check failed")

    async def _check_active_sink(self) -> None:
        page = await self.refresh(publish=False)
        active = next((zone for zone in page.items if zone.active), None)
        if active is None or active.kind == "browser":
            self._sink_warning_sent = False
            return
        sink_name = active.sink_name
        if sink_name and await self._router.is_available(sink_name):
            self._sink_warning_sent = False
            return
        if not self._sink_warning_sent:
            self._sink_warning_sent = True
            logger.warning(
                "active audio sink disappeared: zone=%s sink=%s",
                active.id,
                sink_name,
            )
            await self._bus.publish(
                "system.warning",
                {
                    "code": "audio_sink_lost",
                    "message": "active audio sink disappeared; falling back to local",
                    "zone_id": active.id,
                },
            )
        if active.id == DEFAULT_ZONE_ID:
            return
        try:
            await self.activate(DEFAULT_ZONE_ID)
            logger.info("audio sink watchdog activated local zone")
        except Exception as exc:
            logger.warning("audio sink watchdog fallback failed: %s", type(exc).__name__)

    # -------------------------------------------------------------------- queries

    async def list(self) -> ZonePage:
        await self.ensure_ready()
        items = await self._repo.list()
        backend = str(getattr(self._router, "backend", "mock"))
        degraded = self._settings.mode == "real" and backend == "mock"
        return ZonePage(
            items=items, total=len(items), backend=backend, degraded=degraded
        )

    async def get(self, zone_id: str) -> AudioZone:
        await self.ensure_ready()
        return await self._require(zone_id)

    async def active(self) -> AudioZone | None:
        await self.ensure_ready()
        active_id = await self._repo.active_zone_id()
        return await self._repo.get(active_id) if active_id else None

    # ------------------------------------------------------------------ mutations

    async def activate(self, zone_id: str) -> AudioZone:
        """Switch the active zone without interrupting the current track."""

        await self.ensure_ready()
        zone = await self._require(zone_id)
        sink_name = zone.sink_name
        if zone.kind == "bt":
            if not sink_name:
                raise UnavailableError(
                    "bluetooth zone has no connected A2DP sink",
                    details={"zone_id": zone.id},
                )
            if not await self._router.activate(sink_name):
                raise UnavailableError(
                    "cannot switch the audio sink",
                    details={"zone_id": zone.id, "sink_name": sink_name},
                )
        elif zone.kind == "local_sink":
            sink_name = await self._resolve_local_sink(zone)
            if not await self._router.activate(sink_name):
                raise UnavailableError(
                    "cannot switch the audio sink",
                    details={"zone_id": zone.id, "sink_name": sink_name},
                )
            if sink_name != zone.sink_name:
                await self._repo.set_sink_name(zone.id, sink_name)
        # A browser zone is virtual: mpv stays the clock and this manager must
        # not touch playback state.

        active = await self._repo.mark_active(zone.id)
        if active is None:  # pragma: no cover - row vanished between reads
            raise NotFoundError(f"unknown zone: {zone_id}", details={"zone_id": zone_id})
        await self._apply_volume(active, sink_name)
        await self._sync_player_zone(active)
        page = await self.list()
        await self._publish(page)
        return active

    async def set_volume(self, zone_id: str, volume: int) -> AudioZone:
        await self.ensure_ready()
        if not 0 <= volume <= 100:
            raise AppError(
                "volume must be between 0 and 100",
                code="validation_error",
                status_code=422,
                details={"volume": volume},
            )
        try:
            updated = await self._repo.set_volume(zone_id, volume)
        except ValueError as exc:  # defensive: route validation normally wins
            raise AppError(
                str(exc),
                code="validation_error",
                status_code=422,
                details={"volume": volume},
            ) from exc
        if updated is None:
            raise NotFoundError(f"unknown zone: {zone_id}", details={"zone_id": zone_id})
        if updated.sink_name:
            await self._router.set_volume(updated.sink_name, volume)
        if updated.active and self._player is not None:
            await self._player.set_volume(volume)
        page = await self.list()
        await self._publish(page)
        return updated

    async def set_offset(self, zone_id: str, offset_ms: int) -> AudioZone:
        """Persist the karaoke delay offset; it is not applied to audio here."""

        await self.ensure_ready()
        if not ZONE_OFFSET_MIN_MS <= offset_ms <= ZONE_OFFSET_MAX_MS:
            raise AppError(
                f"offset_ms must be between {ZONE_OFFSET_MIN_MS} and {ZONE_OFFSET_MAX_MS}",
                code="validation_error",
                status_code=422,
                details={"offset_ms": offset_ms},
            )
        try:
            updated = await self._repo.set_offset(zone_id, offset_ms)
        except ValueError as exc:  # defensive: route validation normally wins
            raise AppError(
                str(exc),
                code="validation_error",
                status_code=422,
                details={"offset_ms": offset_ms},
            ) from exc
        if updated is None:
            raise NotFoundError(f"unknown zone: {zone_id}", details={"zone_id": zone_id})
        page = await self.list()
        await self._publish(page)
        return updated

    # --------------------------------------------------------------- bluetooth

    async def attach_bluetooth(self, device: BluetoothDevice) -> AudioZone:
        """Register a connected A2DP device as the concrete ``bt`` zone sink."""

        await self.ensure_ready()
        sink_name = f"{BT_SINK_PREFIX}{device.address.replace(':', '_')}.1"
        # PipeWire usually names the sink after the MAC, but the profile suffix
        # (a2dp-sink vs a2dp-sink-sbc) may differ; prefer a real match when one
        # is already visible.
        try:
            sinks = await self._router.list_sinks()
        except Exception:  # pragma: no cover - router failure is not fatal
            sinks = []
        prefix = f"{BT_SINK_PREFIX}{device.address.replace(':', '_')}"
        match = next((sink.name for sink in sinks if sink.name.startswith(prefix)), None)
        if match:
            sink_name = match
        updated = await self._repo.set_sink_name(BT_ZONE_ID, sink_name)
        if updated is None:  # pragma: no cover - defaults always seed the row
            raise NotFoundError(f"unknown zone: {BT_ZONE_ID}")
        await self._repo.touch(BT_ZONE_ID)
        page = await self.list()
        await self._publish(page)
        return await self._require(BT_ZONE_ID)

    async def wait_bluetooth_sink(
        self, device: BluetoothDevice, timeout_s: float = 4.0
    ) -> bool:
        """Wait briefly for PipeWire to publish the device's A2DP sink.

        BlueZ reports the link before WirePlumber creates the sink; activating a
        zone too early would fail with a confusing ``set-default-sink`` error.
        """

        prefix = f"{BT_SINK_PREFIX}{device.address.replace(':', '_')}"
        if str(getattr(self._router, "backend", "mock")) == "mock":
            # The mock router accepts any sink name; there is nothing to wait for.
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            sinks = await self._router.list_sinks()
            if any(sink.name.startswith(prefix) for sink in sinks):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def detach_bluetooth(self) -> AudioZone | None:
        """Forget the A2DP sink; the ``bt`` row stays as an unavailable placeholder."""

        await self.ensure_ready()
        updated = await self._repo.set_sink_name(BT_ZONE_ID, None)
        await self._repo.touch(BT_ZONE_ID)
        page = await self.list()
        await self._publish(page)
        return updated

    async def auto_reconnect_bluetooth(self, service: Any) -> list[BluetoothDevice]:
        """Best-effort reconnect of paired devices; not scheduled automatically."""

        reconnect = getattr(service, "auto_reconnect", None)
        if not callable(reconnect):
            return []
        devices: list[BluetoothDevice] = await reconnect()
        for device in devices:
            if device.connected:
                await self.attach_bluetooth(device)
        return devices

    # ------------------------------------------------------------------ internals

    async def _require(self, zone_id: str) -> AudioZone:
        zone = await self._repo.get(zone_id)
        if zone is None:
            raise NotFoundError(
                f"unknown zone: {zone_id}", details={"zone_id": zone_id}
            )
        return zone

    async def _resolve_local_sink(self, zone: AudioZone) -> str:
        sinks = await self._router.list_sinks()
        sink_names = {sink.name for sink in sinks}
        if zone.sink_name in sink_names:
            return zone.sink_name  # type: ignore[return-value]
        preferred = self._preferred_sink(sinks)
        if preferred is None:
            raise UnavailableError(
                "no audio sink is available", details={"zone_id": zone.id}
            )
        return preferred

    @staticmethod
    def _preferred_sink(sinks: list[SinkInfo]) -> str | None:
        """Prefer a wired sink over Bluetooth; keep the first usable fallback."""

        for sink in sinks:
            if sink.available and not sink.name.startswith(BT_SINK_PREFIX):
                return sink.name
        for sink in sinks:
            if sink.available:
                return sink.name
        return None

    async def _apply_volume(self, zone: AudioZone, sink_name: str | None) -> None:
        if sink_name:
            await self._router.set_volume(sink_name, zone.volume)
        if self._player is not None:
            # Zone volume becomes mpv's master volume; playback keeps running.
            await self._player.set_volume(zone.volume)

    async def _sync_player_zone(self, zone: AudioZone) -> None:
        """Tell the player which zone is active (browser zone mutes mpv)."""

        setter = getattr(self._player, "set_zone_context", None)
        if not callable(setter):
            return
        try:
            await setter(zone.id, browser=zone.kind == "browser")
        except Exception as exc:  # pragma: no cover - player boundary is defensive
            logger.warning("cannot sync player zone %s: %s", zone.id, exc)

    async def _publish(self, page: ZonePage) -> None:
        await self._bus.publish(
            "zones.updated", {"zones": page.model_dump(mode="json")}
        )


def create_zone_manager(
    settings: Settings,
    bus: EventBus,
    db: Database,
    player: Any | None = None,
    *,
    router: ZoneRouter | None = None,
    runner: CommandRunner | None = None,
) -> ZoneManager:
    """Wire a manager with the router implementation selected by config."""

    return ZoneManager(
        settings=settings,
        bus=bus,
        db=db,
        router=router or create_zone_router(settings, runner=runner),
        player=player,
    )


__all__ = [
    "BT_ZONE_ID",
    "ZoneManager",
    "create_zone_manager",
]
