"""Wave tests: rotor start, feedback semantics, refill and ``wave.updated``.

The fake client records every call, which pins the exact gate-documented
signatures: ``rotor_station_tracks(station, queue=...)`` and
``rotor_station_feedback(station, "skip", track_id=..., total_played_seconds=...)``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.core.event_bus import EventBus
from app.database.connection import Database
from app.database.repositories import TrackRepository
from app.errors import AppError, NotFoundError
from app.integrations.yandex_music.mock_adapter import MockYandexAdapter
from app.integrations.yandex_music.repository import YandexRepository
from app.integrations.yandex_music.wave import WaveService
from tests.p3_yandex_fixtures import (
    fake_album,
    fake_artist,
    fake_station_result,
    fake_track,
)


class _FakeRotor:
    """Only the confirmed rotor/account calls WaveService is allowed to make."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.station_calls: list[tuple[str, str | None]] = []
        self.feedback_calls: list[dict[str, Any]] = []
        self.like_calls: list[list[Any]] = []
        self.unlike_calls: list[list[Any]] = []
        self.dislike_calls: list[list[Any]] = []

    async def rotor_station_tracks(
        self, station: str, *, queue: str | int | None = None
    ) -> Any | None:
        self.station_calls.append((station, None if queue is None else str(queue)))
        return self._results.pop(0) if self._results else None

    async def rotor_feedback(
        self,
        station: str,
        type_: str,
        *,
        track_id: str | int | None = None,
        total_played_seconds: float | None = None,
        batch_id: str | None = None,
    ) -> bool:
        self.feedback_calls.append(
            {
                "station": station,
                "type": type_,
                "track_id": track_id,
                "played_seconds": total_played_seconds,
                "batch_id": batch_id,
            }
        )
        return True

    async def likes_add(self, track_ids: list[Any]) -> bool:
        self.like_calls.append(list(track_ids))
        return True

    async def likes_remove(self, track_ids: list[Any]) -> bool:
        self.unlike_calls.append(list(track_ids))
        return True

    async def dislikes_add(self, track_ids: list[Any]) -> bool:
        self.dislike_calls.append(list(track_ids))
        return True

    async def dislikes_remove(self, track_ids: list[Any]) -> bool:
        return True


def _rotor_track(track_id: int, title: str) -> SimpleNamespace:
    return fake_track(
        track_id,
        title,
        artists=(fake_artist(8001, "Artist"),),
        albums=(fake_album(5001, "Album"),),
        duration_ms=200_000,
    )


def _wave_tracks(track_ids: list[int]) -> list[SimpleNamespace]:
    return [_rotor_track(track_id, f"Волна {track_id}") for track_id in track_ids]


async def _service(
    db: Database, rotor: _FakeRotor, bus: EventBus | None = None
) -> tuple[WaveService, YandexRepository]:
    repository = YandexRepository(db, TrackRepository(db))
    return WaveService(rotor, repository, bus), repository  # type: ignore[arg-type]


async def test_wave_start_normalizes_tracks_and_publishes(db: Database) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    rotor = _FakeRotor(
        [
            fake_station_result(
                _wave_tracks([1001, 1002, 1003, 1004]), batch_id="b1", session_id="s1"
            )
        ]
    )
    service, repository = await _service(db, rotor, bus)

    state = await service.start()
    assert state.station == "user:onyourwave"
    assert state.batch_id == "b1"
    assert state.session_id == "s1"
    assert state.current_index == 0
    assert len(state.tracks) == 4
    assert all(track.source == "yandex" for track in state.tracks)
    assert all(track.external_id for track in state.tracks)
    assert rotor.station_calls == [("user:onyourwave", None)]

    # Internal tracks really exist in SQLite.
    tracks = TrackRepository(db)
    for dto in state.tracks:
        assert await tracks.get(dto.id) is not None

    message = subscription.queue.get_nowait()
    assert message["type"] == "wave.updated"
    assert len(message["payload"]["wave"]["tracks"]) == 4
    assert await repository.get_external_id(state.tracks[0].id) == "1001:5001"


async def test_wave_skip_uses_confirmed_feedback_signature(db: Database) -> None:
    rotor = _FakeRotor(
        [
            fake_station_result(
                _wave_tracks([1001, 1002, 1003, 1004]), batch_id="b1", session_id="s1"
            )
        ]
    )
    service, _ = await _service(db, rotor)
    state = await service.start()

    updated = await service.feedback(
        action="skip", track_id=state.tracks[0].id, played_seconds=42.5
    )
    assert rotor.feedback_calls == [
        {
            "station": "user:onyourwave",
            "type": "skip",
            "track_id": "1001",
            "played_seconds": 42.5,
            "batch_id": "b1",
        }
    ]
    assert updated.current_index == 1
    assert rotor.station_calls == [("user:onyourwave", None)]  # no refill needed
    assert updated.updated_at


async def test_wave_like_toggles_account_likes(db: Database) -> None:
    rotor = _FakeRotor(
        [fake_station_result(_wave_tracks([1001, 1002, 1003]), batch_id="b1", session_id="s1")]
    )
    service, _ = await _service(db, rotor)
    state = await service.start()
    track_id = state.tracks[0].id

    liked = await service.feedback(action="like", track_id=track_id)
    assert rotor.like_calls == [["1001"]]
    assert liked.tracks[0].liked is True

    unliked = await service.feedback(action="like", track_id=track_id)
    assert rotor.unlike_calls == [["1001"]]
    assert unliked.tracks[0].liked is False

    disliked = await service.feedback(action="dislike", track_id=track_id)
    assert rotor.dislike_calls == [["1001"]]
    assert disliked.tracks[0].liked is False
    # Like/dislike are account calls, never rotor feedback (YANDEX_API §4).
    assert rotor.feedback_calls == []


async def test_wave_refills_through_queue_when_short(db: Database) -> None:
    rotor = _FakeRotor(
        [
            fake_station_result(_wave_tracks([1001, 1002]), batch_id="b1", session_id="s1"),
            fake_station_result(
                _wave_tracks([2001, 2002, 2003]), batch_id="b2", session_id="s2"
            ),
        ]
    )
    service, _ = await _service(db, rotor)
    state = await service.start()
    assert len(state.tracks) == 2

    updated = await service.feedback(action="skip", track_id=state.tracks[0].id)
    assert rotor.station_calls == [("user:onyourwave", None), ("user:onyourwave", "1002")]
    assert len(updated.tracks) >= 3
    assert updated.batch_id == "b2"
    assert updated.session_id == "s2"


async def test_wave_rejects_feedback_before_start(db: Database) -> None:
    rotor = _FakeRotor([])
    service, _ = await _service(db, rotor)
    with pytest.raises(AppError) as exc_info:
        await service.feedback(action="skip", track_id=1)
    assert exc_info.value.code == "wave_not_started"


async def test_wave_rejects_unknown_track_and_bad_action(db: Database) -> None:
    rotor = _FakeRotor(
        [fake_station_result(_wave_tracks([1001, 1002, 1003]), batch_id="b1", session_id="s1")]
    )
    service, _ = await _service(db, rotor)
    await service.start()

    with pytest.raises(NotFoundError) as exc_info:
        await service.feedback(action="like", track_id=987_654)
    assert exc_info.value.code == "wave_track_unknown"

    with pytest.raises(AppError) as exc_info:
        await service.feedback(action="dance", track_id=1)
    assert exc_info.value.code == "invalid_wave_action"


async def test_mock_adapter_wave_works_without_network(db: Database) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    adapter = MockYandexAdapter(YandexRepository(db, TrackRepository(db)), bus=bus)

    state = await adapter.wave_start()
    assert state.batch_id == "wave-20261114T093000Z-0001"
    assert len(state.tracks) == 3
    assert all(track.source == "yandex" for track in state.tracks)
    assert [track.title for track in state.tracks] == [
        "Городские огни",
        "Полночный эфир",
        "Тихий дождь",
    ]

    # The mock fixture likes every track, so the first like is a toggle off.
    target = state.tracks[0]
    unliked = await adapter.wave_feedback(action="like", track_id=target.id)
    assert next(t for t in unliked.tracks if t.id == target.id).liked is False
    liked = await adapter.wave_feedback(action="like", track_id=target.id)
    assert next(t for t in liked.tracks if t.id == target.id).liked is True

    skipped = await adapter.wave_feedback(
        action="skip", track_id=target.id, played_seconds=12
    )
    assert skipped.current_index >= 1
    assert len(skipped.tracks) >= 3
    assert await TrackRepository(db).get(target.id) is not None

    events = []
    while not subscription.queue.empty():
        events.append(subscription.queue.get_nowait())
    assert [event["type"] for event in events] == ["wave.updated"] * len(events)
    assert len(events) == 4
