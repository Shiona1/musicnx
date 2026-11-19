"""Mock Yandex Music adapter.

The real adapter (device auth, catalog, streaming, wave) is P3. This one is
honest: it reports ``available: false`` and only supplies the local mock catalog
that seeds the development database. It never fakes network state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MockTrack:
    """A seed catalog entry. Not a Yandex Music object."""

    external_id: str
    title: str
    artist: str
    album: str
    year: int
    duration_ms: int
    genre: str
    track_number: int
    disc_number: int


MOCK_CATALOG: tuple[MockTrack, ...] = (
    MockTrack(
        external_id="mock:track:1",
        title="Ночной эфир",
        artist="Тестовый Оркестр",
        album="Mock Album",
        year=2024,
        duration_ms=214_000,
        genre="Electronic",
        track_number=1,
        disc_number=1,
    ),
    MockTrack(
        external_id="mock:track:2",
        title="Северное сияние",
        artist="Тестовый Оркестр",
        album="Mock Album",
        year=2024,
        duration_ms=198_000,
        genre="Electronic",
        track_number=2,
        disc_number=1,
    ),
    MockTrack(
        external_id="mock:track:3",
        title="Городские огни",
        artist="Mock Artist",
        album="Cities",
        year=2023,
        duration_ms=245_000,
        genre="Synthwave",
        track_number=1,
        disc_number=1,
    ),
    MockTrack(
        external_id="mock:track:4",
        title="Тихий дождь",
        artist="Mock Artist",
        album="Cities",
        year=2023,
        duration_ms=176_000,
        genre="Ambient",
        track_number=2,
        disc_number=1,
    ),
    MockTrack(
        external_id="mock:track:5",
        title="Пыль дорог",
        artist="Тестовый Оркестр",
        album="Roads",
        year=2022,
        duration_ms=305_000,
        genre="Rock",
        track_number=1,
        disc_number=1,
    ),
    MockTrack(
        external_id="mock:track:6",
        title="Короткая проверка",
        artist="Mock Artist",
        album="Tests",
        year=2024,
        duration_ms=4_000,
        genre="Test",
        track_number=1,
        disc_number=1,
    ),
)


class YandexAdapter(Protocol):
    """Boundary every Yandex implementation must honour."""

    async def describe(self) -> dict[str, Any]: ...


class MockYandexAdapter:
    """P0/P1 adapter: reports unavailability and provides seed metadata."""

    backend = "mock"

    async def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "available": False,
            "authenticated": False,
            "note": "Yandex integration is not implemented before P3",
        }

    def catalog(self) -> tuple[MockTrack, ...]:
        return MOCK_CATALOG
