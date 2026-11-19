"""PlayerManager: owns playback state, drives the audio backend, emits events.

The manager is the only writer of ``playback_state``. Position is persisted with
a debounce (default 7 s) plus every status/track/seek change, never per tick
(SPEC §11.2).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.audio.base import AudioBackend
from app.audio.factory import PlayableSourceResolver
from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import (
    PlayerState,
    PlayerStatus,
    QueueItem,
    QueueState,
    RepeatMode,
    Track,
)
from app.core.queue_manager import QueueManager
from app.core.state_machine import ensure_transition
from app.core.timeutils import utc_now_iso
from app.database.repositories import (
    PlaybackStateRepository,
    PlayHistoryRepository,
    SavedPlaybackState,
    SettingsRepository,
    TrackRepository,
)
from app.errors import AppError, ConflictError, NotFoundError, UnavailableError
from app.logging_setup import get_logger

logger = get_logger("core.player")

TICK_INTERVAL_S = 0.25
VOLUME_SETTING_KEY = "player.volume"
SHUFFLE_SETTING_KEY = "player.shuffle"
REPEAT_SETTING_KEY = "player.repeat"
#: How long a failed CDN probe suppresses further online attempts. Long enough
#: to skip a whole online queue instantly, short enough to recover by itself.
CDN_BLOCK_TTL_S = 300.0


@dataclass
class _State:
    status: PlayerStatus = "stopped"
    track_id: int | None = None
    queue_item_id: int | None = None
    position_ms: int = 0
    duration_ms: int = 0
    volume: int = 70
    zone_id: str | None = "local"
    queue_index: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)


class PlayerManager:
    def __init__(
        self,
        *,
        settings: Settings,
        bus: EventBus,
        queue: QueueManager,
        tracks: TrackRepository,
        state_repo: PlaybackStateRepository,
        settings_repo: SettingsRepository,
        history: PlayHistoryRepository,
        backend: AudioBackend,
        resolver: PlayableSourceResolver,
        fallback: Any | None = None,
    ) -> None:
        self._settings = settings
        self._bus = bus
        self._queue = queue
        self._tracks = tracks
        self._state_repo = state_repo
        self._settings_repo = settings_repo
        self._history = history
        self._backend = backend
        self._resolver = resolver
        self._fallback = fallback
        self._lock = asyncio.Lock()
        self._state = _State(volume=settings.player.default_volume)
        # Runtime browser-zone flag: the zone manager flips it when the active
        # output becomes the browser. mpv keeps the clock but is muted.
        self._browser_zone = bool(settings.audio.browser_zone)
        # Transport modes, restored from the settings store like volume.
        self._shuffle = False
        self._repeat: RepeatMode = "off"
        self._cdn_blocked_until = 0.0
        self._ticker: asyncio.Task[None] | None = None
        self._last_emit = 0.0
        self._last_persist = 0.0

    # ------------------------------------------------------------------ lifecycle

    def start_ticker(self) -> None:
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._run_ticker(), name="player-ticker")

    async def shutdown(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except asyncio.CancelledError:
                pass
            self._ticker = None
        async with self._lock:
            if self._state.status == "playing":
                self._state.position_ms = self._backend.get_position_ms()
            await self._persist_locked()

    async def restore(self) -> None:
        """Restore queue/state after restart; never starts audio (ADR-004)."""

        async with self._lock:
            saved = await self._state_repo.load()
            items = await self._queue.items()
            volume = saved.volume if saved is not None else self._settings.player.default_volume
            stored_volume = await self._settings_repo.get(VOLUME_SETTING_KEY)
            if isinstance(stored_volume, int) and 0 <= stored_volume <= 100:
                volume = stored_volume

            zone_id = (saved.zone_id if saved else None) or "local"
            stored_shuffle = await self._settings_repo.get(SHUFFLE_SETTING_KEY)
            if stored_shuffle in {"on", "off"}:
                self._shuffle = stored_shuffle == "on"
            stored_repeat = await self._settings_repo.get(REPEAT_SETTING_KEY)
            if stored_repeat in {"off", "one", "all"}:
                self._repeat = stored_repeat
            self._state = _State(volume=volume, zone_id=zone_id)
            self._browser_zone = (
                bool(self._settings.audio.browser_zone) or zone_id == "browser"
            )
            if saved is not None and saved.track_id is not None:
                index = self._find_index(items, saved.queue_item_id, saved.track_id)
                if index is not None:
                    item = items[index]
                    duration = item.track.duration_ms or 0
                    position = min(saved.position_ms, duration) if duration else saved.position_ms
                    status: PlayerStatus = (
                        "paused"
                        if saved.status in {"playing", "buffering", "paused", "error"}
                        else "stopped"
                    )
                    self._state.track_id = item.track.id
                    self._state.queue_item_id = item.id
                    self._state.queue_index = index
                    self._state.duration_ms = duration
                    self._state.position_ms = position
                    self._state.status = status
            self._state.updated_at = utc_now_iso()
            await self._backend.set_volume(volume)
            await self._persist_locked()

    # ------------------------------------------------------------------ public API

    async def snapshot(self) -> PlayerState:
        async with self._lock:
            return await self._build_state_locked()

    @property
    def browser_zone(self) -> bool:
        """True while the active output is the browser (mpv muted, client plays)."""

        return self._browser_zone

    async def set_zone_context(self, zone_id: str, *, browser: bool) -> PlayerState:
        """Follow the active output zone without stopping the current track.

        The browser zone is virtual: mpv remains the master clock but its local
        output is muted, and clients stream the same track through ``/stream``.
        """

        async with self._lock:
            changed = self._state.zone_id != zone_id or self._browser_zone != browser
            self._state.zone_id = zone_id
            self._browser_zone = browser
            await self._backend.set_muted(browser)
            if changed:
                self._state.updated_at = utc_now_iso()
                await self._emit_state_locked()
            return await self._build_state_locked()

    async def queue_state(self) -> QueueState:
        async with self._lock:
            return await self._queue.snapshot(self._state.queue_item_id)

    async def play(self, track_id: int | None = None) -> PlayerState:
        async with self._lock:
            items = await self._queue.items()
            if track_id is not None:
                index = next(
                    (i for i, item in enumerate(items) if item.track.id == track_id), None
                )
                if index is None:
                    if not await self._tracks.exists(track_id):
                        raise NotFoundError("track not found", details={"track_id": track_id})
                    await self._queue.add_tracks([track_id])
                    items = await self._queue.items()
                    index = len(items) - 1
                await self._play_item_locked(index, 0, items=items)
            elif self._state.status == "playing":
                pass
            elif self._state.queue_item_id is not None:
                index = self._index_of_item(items, self._state.queue_item_id)
                if index is None:
                    index = 0 if items else None
                if index is not None:
                    start_ms = self._state.position_ms if self._state.status != "playing" else 0
                    await self._play_item_locked(
                    index,
                    start_ms,
                    items=items,
                    track_changed=self._state.status == "stopped",
                    record_history=self._state.status == "stopped",
                )
            elif items:
                await self._play_item_locked(0, 0, items=items)
            else:
                raise ConflictError("queue is empty", code="queue_empty")
            return await self._build_state_locked()

    async def pause(self) -> PlayerState:
        async with self._lock:
            if self._state.status in {"playing", "buffering"}:
                position = (
                    self._backend.get_position_ms()
                    if self._state.status == "playing"
                    else self._state.position_ms
                )
                await self._backend.pause()
                self._set_status_locked("paused")
                self._state.position_ms = max(position, 0)
                self._state.updated_at = utc_now_iso()
                await self._persist_locked()
                await self._emit_state_locked()
            return await self._build_state_locked()

    async def stop(self) -> PlayerState:
        async with self._lock:
            await self._stop_locked()
            return await self._build_state_locked()

    async def next_track(self) -> PlayerState:
        async with self._lock:
            items = await self._queue.items()
            index = self._index_of_item(items, self._state.queue_item_id)
            if self._shuffle and len(items) > 1:
                # Manual Next under shuffle jumps to a random playable item,
                # matching what auto-advance would have chosen.
                start = self._pick_shuffled_index(items, index)
            else:
                start = 0 if index is None else index + 1
                if start >= len(items) and self._repeat == "all" and items:
                    start = 0
            await self._advance_locked(items, start)
            return await self._build_state_locked()

    async def previous_track(self) -> PlayerState:
        # Previous always walks queue order; shuffle only affects forward
        # selection. With no played-track history this is the honest v1.
        async with self._lock:
            items = await self._queue.items()
            position = (
                self._backend.get_position_ms()
                if self._state.status == "playing"
                else self._state.position_ms
            )
            threshold = self._settings.player.previous_restart_threshold_ms
            index = self._index_of_item(items, self._state.queue_item_id)
            if position > threshold or index is None or index == 0:
                await self._seek_locked(0)
            else:
                await self._play_item_locked(index - 1, 0, items=items)
            return await self._build_state_locked()

    async def seek(self, position_ms: int) -> PlayerState:
        async with self._lock:
            await self._seek_locked(position_ms)
            return await self._build_state_locked()

    async def set_volume(self, volume: int) -> PlayerState:
        if not 0 <= volume <= 100:
            raise AppError(
                "volume must be between 0 and 100",
                code="validation_error",
                status_code=422,
                details={"volume": volume},
            )
        async with self._lock:
            await self._backend.set_volume(volume)
            self._state.volume = volume
            self._state.updated_at = utc_now_iso()
            await self._settings_repo.set(VOLUME_SETTING_KEY, volume)
            await self._persist_locked()
            await self._bus.publish("volume.changed", {"volume": volume})
            await self._emit_state_locked()
            return await self._build_state_locked()

    async def set_mode(
        self,
        *,
        shuffle: bool | None = None,
        repeat: RepeatMode | None = None,
    ) -> PlayerState:
        """Change shuffle/repeat and persist both, like volume.

        Playback logic reacts on the next advance: no track restarts here.
        """

        if shuffle is None and repeat is None:
            raise AppError(
                "at least one of shuffle or repeat is required",
                code="validation_error",
                status_code=422,
            )
        async with self._lock:
            if shuffle is not None:
                self._shuffle = shuffle
                await self._settings_repo.set(
                    SHUFFLE_SETTING_KEY, "on" if shuffle else "off"
                )
            if repeat is not None:
                self._repeat = repeat
                await self._settings_repo.set(REPEAT_SETTING_KEY, repeat)
            self._state.updated_at = utc_now_iso()
            await self._persist_locked()
            await self._emit_state_locked()
            return await self._build_state_locked()

    async def add_to_queue(self, track_ids: list[int]) -> QueueState:
        async with self._lock:
            await self._queue.add_tracks(track_ids)
            await self._publish_queue_locked()
            await self._emit_state_locked()
            return await self._queue.snapshot(self._state.queue_item_id)

    async def remove_queue_item(self, item_id: int) -> QueueState:
        async with self._lock:
            removed = await self._queue.remove_item(item_id)
            items = await self._queue.items()
            was_current = self._state.queue_item_id == item_id
            if was_current:
                await self._backend.stop()
                if items:
                    index = min(removed.position, len(items) - 1)
                    item = items[index]
                    self._set_status_locked("stopped")
                    self._state.track_id = item.track.id
                    self._state.queue_item_id = item.id
                    self._state.queue_index = index
                    self._state.duration_ms = item.track.duration_ms or 0
                    self._state.position_ms = 0
                else:
                    self._set_status_locked("stopped")
                    self._state.track_id = None
                    self._state.queue_item_id = None
                    self._state.queue_index = None
                    self._state.duration_ms = 0
                    self._state.position_ms = 0
                self._state.updated_at = utc_now_iso()
                await self._persist_locked()
                await self._emit_state_locked(track_changed=True)
            else:
                self._state.queue_index = self._index_of_item(
                    items, self._state.queue_item_id
                )
                await self._emit_state_locked()
            await self._publish_queue_locked()
            return await self._queue.snapshot(self._state.queue_item_id)

    async def reorder_queue(self, item_ids: list[int]) -> QueueState:
        async with self._lock:
            await self._queue.reorder(item_ids)
            items = await self._queue.items()
            self._state.queue_index = self._index_of_item(items, self._state.queue_item_id)
            await self._publish_queue_locked()
            await self._emit_state_locked()
            return await self._queue.snapshot(self._state.queue_item_id)

    async def clear_queue(self) -> QueueState:
        """Drop upcoming items, keeping the current one (Spotify semantic).

        The current item keeps playing; with nothing current the whole queue
        is removed.
        """

        async with self._lock:
            await self._queue.clear_except_current(self._state.queue_item_id)
            self._state.updated_at = utc_now_iso()
            await self._emit_state_locked()
            await self._publish_queue_locked()
            return await self._queue.snapshot(self._state.queue_item_id)

    # ------------------------------------------------------------------ internals

    def _set_status_locked(self, target: PlayerStatus) -> None:
        if self._state.status == target:
            return
        ensure_transition(self._state.status, target)
        self._state.status = target
        self._state.updated_at = utc_now_iso()

    # ------------------------------------------------------- source availability

    def _cdn_blocked(self) -> bool:
        return time.monotonic() < self._cdn_blocked_until

    def _mark_cdn_unreachable(self) -> None:
        self._cdn_blocked_until = time.monotonic() + CDN_BLOCK_TTL_S

    def _clear_cdn_block(self) -> None:
        self._cdn_blocked_until = 0.0

    @staticmethod
    def _needs_cdn(track: Track) -> bool:
        """True when playing the track requires the Yandex CDN."""

        return track.source == "yandex" and not (
            track.has_local_file and not track.file_missing
        )

    def _is_online_blocked(self, track: Track) -> bool:
        return self._cdn_blocked() and self._needs_cdn(track)

    @staticmethod
    def _is_known_unplayable(track: Track) -> bool:
        return track.file_missing or track.source_unavailable

    def _next_playable_index(
        self, items: list[QueueItem], start: int
    ) -> int | None:
        """First index at/after ``start`` that is not known to be unplayable."""

        for index in range(max(start, 0), len(items)):
            track = items[index].track
            if self._is_known_unplayable(track):
                continue
            if self._is_online_blocked(track):
                continue
            return index
        return None

    async def _play_item_locked(
        self,
        index: int,
        start_ms: int,
        *,
        items: list[QueueItem] | None = None,
        track_changed: bool = True,
        record_history: bool = True,
        allow_fallback: bool = True,
    ) -> None:
        items = items if items is not None else await self._queue.items()
        if not items or index < 0 or index >= len(items):
            raise ConflictError("queue is empty", code="queue_empty")
        item = items[index]
        track: Track = item.track
        duration = track.duration_ms or 0
        position = min(max(start_ms, 0), duration) if duration else max(start_ms, 0)

        self._set_status_locked("buffering")
        self._state.error_code = None
        self._state.error_detail = None
        self._state.track_id = track.id
        self._state.queue_item_id = item.id
        self._state.queue_index = index
        self._state.duration_ms = duration
        self._state.position_ms = position
        self._state.updated_at = utc_now_iso()
        await self._emit_state_locked()

        try:
            if self._is_online_blocked(track):
                raise UnavailableError(
                    "Yandex CDN is unreachable; online track skipped",
                    code="cdn_unreachable",
                    details={"track_id": track.id, "cached": True},
                )
            uri = await self._resolver.resolve(track)
            await self._backend.load(uri, start_ms=position, duration_ms=duration or None)
            await self._backend.set_volume(self._state.volume)
            # Browser zone: mpv remains the clock, but its local output must not
            # double-play while clients stream the same track.
            await self._backend.set_muted(self._browser_zone)
            await self._backend.play()
        except AppError as exc:
            code = getattr(exc, "code", "cannot_start_playback")
            if code == "cdn_unreachable":
                self._mark_cdn_unreachable()
            if allow_fallback and self._fallback is not None and code in {
                "cdn_unreachable",
                "no_playable_source",
                "source_unreachable",
                "stream_unavailable",
            }:
                alternate = None
                try:
                    alternate = await self._fallback.find(
                        track, allow_online=not self._cdn_blocked()
                    )
                except Exception as fallback_exc:  # never mask the original error
                    logger.warning(
                        "track fallback failed: track_id=%s reason=%s",
                        track.id,
                        type(fallback_exc).__name__,
                    )
                if alternate is not None and alternate.id != track.id:
                    await self._queue.replace_track(item.id, alternate.id)
                    await self._bus.publish(
                        "queue.updated",
                        {"queue": await self._queue.snapshot(self._state.queue_item_id)},
                    )
                    await self._bus.publish(
                        "system.warning",
                        {
                            "code": "track_fallback",
                            "track_id": track.id,
                            "fallback_track_id": alternate.id,
                            "title": alternate.title,
                        },
                    )
                    await self._play_item_locked(
                        index,
                        0,
                        items=await self._queue.items(),
                        allow_fallback=False,
                    )
                    return
            await self._fail_locked(code, str(exc) or None)
            raise
        except Exception as exc:
            detail = str(exc)
            lowered = detail.lower()
            code = (
                "source_unreachable"
                if "timed out" in lowered or "loading failed" in lowered
                else "audio_backend_error"
            )
            await self._fail_locked(code, detail or None)
            raise UnavailableError(
                "audio backend is unavailable", details={"reason": detail}
            ) from exc

        if uri.startswith(("http://", "https://")):
            # A live remote stream proves the CDN is back; local tracks do not.
            self._clear_cdn_block()

        # mpv may know the duration even when the catalog has no metadata yet.
        backend_duration = self._backend.get_duration_ms()
        if backend_duration > 0:
            self._state.duration_ms = backend_duration

        self._set_status_locked("playing")
        if record_history:
            await self._history.add(track.id, self._state.zone_id)
        await self._persist_locked()
        await self._emit_state_locked(track_changed=track_changed)
        await self._publish_queue_locked()

    async def _stop_locked(self) -> None:
        if self._state.status != "stopped":
            await self._backend.stop()
            self._set_status_locked("stopped")
            self._state.position_ms = 0
            self._state.error_code = None
            self._state.error_detail = None
            self._state.updated_at = utc_now_iso()
            await self._persist_locked()
            await self._emit_state_locked()

    async def _seek_locked(self, position_ms: int) -> None:
        if self._state.track_id is None:
            raise ConflictError("nothing is playing", code="nothing_to_seek")
        clamped = max(position_ms, 0)
        if self._state.duration_ms:
            clamped = min(clamped, self._state.duration_ms)
        if self._state.status in {"playing", "paused", "buffering"}:
            try:
                await self._backend.seek(clamped)
            except Exception as exc:
                logger.warning("audio backend rejected seek: %s", exc)
                raise UnavailableError(
                    "audio backend rejected the seek",
                    details={"position_ms": clamped, "reason": str(exc)},
                ) from exc
        self._state.position_ms = clamped
        self._state.updated_at = utc_now_iso()
        await self._persist_locked()
        await self._emit_state_locked()
        await self._publish_position_locked()

    async def _fail_locked(self, code: str, detail: str | None = None) -> None:
        try:
            self._set_status_locked("error")
        except Exception:  # pragma: no cover - defensive
            self._state.status = "error"
        self._state.error_code = code
        self._state.error_detail = detail
        self._state.updated_at = utc_now_iso()
        await self._persist_locked()
        await self._bus.publish(
            "system.warning",
            {"code": code, "message": detail or code},
        )
        await self._emit_state_locked()

    async def _handle_eof_locked(self) -> None:
        items = await self._queue.items()
        index = self._index_of_item(items, self._state.queue_item_id)
        if not self._settings.player.autoplay or index is None:
            await self._stop_locked()
            return
        if self._repeat == "one":
            # Replay without a track_changed event or a history entry: it is
            # the same track looping, not a new play.
            await self._play_item_locked(
                index, 0, items=items, track_changed=False, record_history=False
            )
            return
        if self._shuffle and len(items) > 1:
            await self._advance_locked(items, self._pick_shuffled_index(items, index))
            return
        start = index + 1
        if start >= len(items):
            if self._repeat != "all":
                await self._stop_locked()
                return
            start = 0
        await self._advance_locked(items, start)

    def _pick_shuffled_index(
        self, items: list[QueueItem], current: int | None
    ) -> int:
        """Random playable index different from ``current``.

        Unplayable candidates are excluded when any playable one exists; the
        current item is never picked, so a 2+ item queue always changes track.
        """

        candidates = [i for i in range(len(items)) if i != current]
        playable = [
            i
            for i in candidates
            if not self._is_known_unplayable(items[i].track)
            and not self._is_online_blocked(items[i].track)
        ]
        pool = playable or candidates
        return random.choice(pool) if pool else (current or 0)

    async def _advance_locked(
        self, items: list[QueueItem], start: int
    ) -> None:
        """Play the next playable track, skipping unreachable sources.

        A failed CDN probe marks the online source blocked for a few minutes;
        the rest of an online queue is then skipped without another timeout.
        Explicit ``play(track_id)`` never uses this path — only auto-advance
        and the Next button do.
        """

        if start >= len(items):
            await self._stop_locked()
            return

        index = self._next_playable_index(items, start)
        skipped = (len(items) - start) if index is None else (index - start)
        while index is not None:
            try:
                await self._play_item_locked(index, 0, items=items)
            except AppError as exc:
                if getattr(exc, "code", "") != "cdn_unreachable":
                    raise
                skipped += 1
                index = self._next_playable_index(items, index + 1)
                continue
            if skipped:
                await self._bus.publish(
                    "system.warning",
                    {
                        "code": "queue_skipped_unavailable",
                        "message": f"skipped {skipped} unavailable track(s)",
                        "skipped": skipped,
                    },
                )
            return

        await self._stop_locked()
        if skipped:
            await self._bus.publish(
                "system.warning",
                {
                    "code": "queue_exhausted_offline",
                    "message": (
                        "no playable track left in the queue "
                        "(remaining tracks are unavailable)"
                    ),
                    "skipped": skipped,
                },
            )

    async def _run_ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL_S)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - logged, keeps the ticker alive
                logger.exception("player ticker failed")

    async def _tick(self) -> None:
        async with self._lock:
            error = self._backend.take_error()
            if error is not None:
                await self._fail_locked("audio_backend_error", error)
                return
            if self._backend.take_eof():
                await self._handle_eof_locked()
                return
            if self._state.status != "playing":
                return

            position = self._backend.get_position_ms()
            duration = self._state.duration_ms or self._backend.get_duration_ms()
            if duration and self._state.duration_ms == 0:
                # The backend (mpv) learned the duration after load; publish it.
                self._state.duration_ms = duration
                self._state.updated_at = utc_now_iso()
                await self._emit_state_locked()
            if duration and position >= duration:
                self._state.position_ms = duration
                await self._handle_eof_locked()
                return

            self._state.position_ms = max(position, 0)
            now = time.monotonic()
            emit_interval = 1.0 / self._settings.player.position_event_hz
            if now - self._last_emit >= emit_interval:
                self._last_emit = now
                await self._publish_position_locked()
            if now - self._last_persist >= self._settings.player.position_persist_interval_s:
                await self._persist_locked()

    async def _persist_locked(self) -> None:
        await self._state_repo.save(
            SavedPlaybackState(
                track_id=self._state.track_id,
                queue_item_id=self._state.queue_item_id,
                position_ms=self._state.position_ms,
                volume=self._state.volume,
                status=self._state.status,
                zone_id=self._state.zone_id,
                queue_index=self._state.queue_index or 0,
                updated_at=utc_now_iso(),
            )
        )
        self._last_persist = time.monotonic()

    async def _publish_position_locked(self) -> None:
        await self._bus.publish(
            "player.position",
            {
                "position_ms": self._state.position_ms,
                "duration_ms": self._state.duration_ms,
                "status": self._state.status,
                "server_time_ms": int(time.time() * 1000),
            },
        )

    async def _publish_queue_locked(self) -> None:
        queue = await self._queue.snapshot(self._state.queue_item_id)
        await self._bus.publish("queue.updated", {"queue": queue.model_dump()})

    async def _emit_state_locked(self, *, track_changed: bool = False) -> None:
        state = await self._build_state_locked()
        payload: dict[str, Any] = {"state": state.model_dump()}
        if track_changed:
            await self._bus.publish("player.track_changed", payload)
        await self._bus.publish("player.state_changed", payload)

    async def _build_state_locked(self) -> PlayerState:
        items = await self._queue.items()
        index = self._index_of_item(items, self._state.queue_item_id)
        item = items[index] if index is not None else None
        if self._state.queue_item_id is not None and item is None:
            self._state.queue_item_id = None
            self._state.track_id = None
            self._state.duration_ms = 0
            self._state.position_ms = 0
        self._state.queue_index = index
        track = item.track if item is not None else None
        if track is not None and track.duration_ms:
            self._state.duration_ms = track.duration_ms
        elif track is not None and self._state.duration_ms <= 0:
            backend_duration = self._backend.get_duration_ms()
            if backend_duration > 0:
                self._state.duration_ms = backend_duration

        can_next = index is not None and (
            index + 1 < len(items)
            or self._repeat == "all"
            or (self._shuffle and len(items) > 1)
        )
        can_previous = bool(items) and (
            (index is not None and index > 0)
            or self._state.position_ms > self._settings.player.previous_restart_threshold_ms
        )
        return PlayerState(
            status=self._state.status,
            track=track,
            position_ms=self._state.position_ms,
            duration_ms=self._state.duration_ms,
            volume=self._state.volume,
            shuffle=self._shuffle,
            repeat=self._repeat,
            zone_id=self._state.zone_id or "local",
            queue_index=index,
            queue_item_id=self._state.queue_item_id,
            can_play=self._state.status != "playing" and (track is not None or bool(items)),
            can_pause=self._state.status in {"playing", "buffering"},
            can_next=can_next,
            can_previous=can_previous,
            error_code=self._state.error_code,
            error_detail=self._state.error_detail,
            updated_at=self._state.updated_at,
        )

    @staticmethod
    def _index_of_item(items: list[QueueItem], item_id: int | None) -> int | None:
        if item_id is None:
            return None
        for index, item in enumerate(items):
            if item.id == item_id:
                return index
        return None

    @staticmethod
    def _find_index(
        items: list[QueueItem], item_id: int | None, track_id: int
    ) -> int | None:
        if item_id is not None:
            for index, item in enumerate(items):
                if item.id == item_id:
                    return index
        for index, item in enumerate(items):
            if item.track.id == track_id:
                return index
        return None
