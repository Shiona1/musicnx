"""«Моя волна» (rotor) session: start, feedback, refill and WS updates.

Station feedback uses only the types frozen in docs/YANDEX_API.md §4
(``radioStarted``/``trackStarted``/``trackFinished``/``skip``). Like/dislike is
honestly an account action through ``users_likes_tracks_add/remove`` and
``users_dislikes_tracks_add``: the rotor feedback types do not include them.
"""

from __future__ import annotations

from app.core.event_bus import EventBus
from app.core.models import Track, WaveState
from app.core.timeutils import utc_now_iso
from app.errors import AppError, NotFoundError, UnavailableError
from app.integrations.yandex_music.client import YandexClient
from app.integrations.yandex_music.mapper import NormalizedWave, map_wave
from app.integrations.yandex_music.repository import YandexRepository
from app.logging_setup import get_logger

logger = get_logger("yandex.wave")

WAVE_STATION = "user:onyourwave"
MIN_WAVE_TRACKS = 3
MAX_WAVE_TRACKS = 50
WAVE_EVENT = "wave.updated"
FEEDBACK_ACTIONS = ("like", "dislike", "skip")


class WaveService:
    """Owns the in-memory wave session (batch/session id, order, liked flags)."""

    def __init__(
        self,
        client: YandexClient,
        repository: YandexRepository,
        bus: EventBus | None = None,
        *,
        station: str = WAVE_STATION,
    ) -> None:
        self._client = client
        self._repository = repository
        self._bus = bus
        self._station = station
        self._state: WaveState | None = None
        self._liked: dict[int, bool] = {}
        self._batch_id: str | None = None
        self._session_id: str | None = None

    @property
    def state(self) -> WaveState | None:
        return self._state

    # ------------------------------------------------------------------ start

    async def start(self) -> WaveState:
        result = await self._client.rotor_station_tracks(self._station)
        if result is None:
            raise UnavailableError(
                "Yandex Music returned no wave tracks",
                code="wave_unavailable",
                details={"station": self._station},
            )
        norms = map_wave(result, self._station)
        internal_ids = await self._repository.upsert_tracks(norms.tracks)
        tracks = await self._repository.list_tracks(internal_ids)
        self._batch_id = norms.batch_id
        self._session_id = norms.session_id
        self._remember(norms, internal_ids)
        self._state = WaveState(
            station=self._station,
            batch_id=self._batch_id,
            session_id=self._session_id,
            tracks=self._with_liked(tracks),
            current_index=0,
            updated_at=utc_now_iso(),
        )
        await self._publish()
        return self._state

    # --------------------------------------------------------------- feedback

    async def feedback(
        self,
        *,
        action: str,
        track_id: int,
        played_seconds: float = 0.0,
    ) -> WaveState:
        if action not in FEEDBACK_ACTIONS:
            raise AppError(
                "unsupported wave feedback action",
                code="invalid_wave_action",
                status_code=400,
                details={"action": action[:32]},
            )
        state = self._state
        if state is None:
            raise AppError(
                "wave is not started",
                code="wave_not_started",
                status_code=409,
            )

        yandex_id = await self._yandex_id(track_id)
        if action == "skip":
            await self._client.rotor_feedback(
                self._station,
                "skip",
                track_id=yandex_id,
                total_played_seconds=max(float(played_seconds), 0.0),
                batch_id=self._batch_id,
            )
        elif action == "like":
            if self._liked.get(track_id, False):
                await self._client.likes_remove([yandex_id])
                self._liked[track_id] = False
            else:
                await self._client.likes_add([yandex_id])
                self._liked[track_id] = True
        else:  # dislike
            await self._client.dislikes_add([yandex_id])
            self._liked[track_id] = False

        if len(state.tracks) - (state.current_index + 1) < MIN_WAVE_TRACKS:
            await self._refill(last_track_id=state.tracks[-1].id if state.tracks else None)

        current_index = self._state.current_index if self._state is not None else 0
        if action == "skip" and self._state is not None:
            current_index = min(
                current_index + 1, max(len(self._state.tracks) - 1, 0)
            )

        updated = self._state.model_copy(
            update={
                "tracks": self._with_liked(self._state.tracks),
                "current_index": current_index,
                "batch_id": self._batch_id,
                "session_id": self._session_id,
                "updated_at": utc_now_iso(),
            }
        )
        self._state = updated
        await self._publish()
        return updated

    # ---------------------------------------------------------------- refill

    async def _refill(self, *, last_track_id: int | None) -> None:
        state = self._state
        if state is None or last_track_id is None:
            return
        queue = await self._repository.get_external_id(last_track_id)
        if queue is None:
            return
        yandex_queue = queue.split(":", 1)[0]
        result = await self._client.rotor_station_tracks(
            self._station, queue=yandex_queue
        )
        if result is None:
            return
        norms = map_wave(result, self._station)
        if not norms.tracks:
            return
        internal_ids = await self._repository.upsert_tracks(norms.tracks)
        fresh = await self._repository.list_tracks(internal_ids)
        known = {track.id for track in state.tracks}
        added = [track for track in fresh if track.id not in known]
        if not added:
            return
        merged = [*state.tracks, *added]
        dropped = max(len(merged) - MAX_WAVE_TRACKS, 0)
        if dropped:
            merged = merged[dropped:]
        self._remember(norms, internal_ids)
        if norms.batch_id:
            self._batch_id = norms.batch_id
        if norms.session_id:
            self._session_id = norms.session_id
        self._state = state.model_copy(
            update={
                "tracks": merged,
                "current_index": max(state.current_index - dropped, 0),
            }
        )

    # ---------------------------------------------------------------- helpers

    async def _yandex_id(self, track_id: int) -> str:
        external = await self._repository.get_external_id(track_id)
        if external is None:
            raise NotFoundError(
                "track is not part of the wave",
                code="wave_track_unknown",
                details={"track_id": track_id},
            )
        return external.split(":", 1)[0]

    def _remember(self, norms: NormalizedWave, internal_ids: list[int]) -> None:
        for index, internal_id in enumerate(internal_ids):
            if index < len(norms.liked):
                self._liked[internal_id] = bool(norms.liked[index])

    def _with_liked(self, tracks: list[Track]) -> list[Track]:
        result: list[Track] = []
        for track in tracks:
            if track.id in self._liked:
                result.append(track.model_copy(update={"liked": self._liked[track.id]}))
            else:
                result.append(track)
        return result

    async def _publish(self) -> None:
        if self._bus is None or self._state is None:
            return
        await self._bus.publish(
            WAVE_EVENT, {"wave": self._state.model_dump(mode="json")}
        )
