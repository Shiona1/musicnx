"""Track fallback: local duplicate first, Yandex alternate second."""

from __future__ import annotations

from typing import Any

from app.core.models import ArtistRef, Track
from app.library.fallback import TrackFallbackService, normalize_text


def _track(
    track_id: int,
    title: str,
    artist: str,
    *,
    local: bool = False,
    online: bool = False,
    duration_ms: int = 200_000,
) -> Track:
    return Track(
        id=track_id,
        title=title,
        artists=[ArtistRef(id=1, name=artist)],
        duration_ms=duration_ms,
        has_local_file=local,
        has_online_source=online,
    )


class FakeTracks:
    def __init__(self, items: list[Track]) -> None:
        self.items = items

    async def list(self, **_: Any) -> tuple[list[Track], int]:
        return self.items, len(self.items)


class FakeYandex:
    def __init__(self, items: list[Track]) -> None:
        self.items = items
        self.queries: list[str] = []

    async def search(self, query: str, **_: Any) -> Any:
        self.queries.append(query)

        class Page:
            class Tracks:
                def __init__(self, inner: list[Track]) -> None:
                    self.items = inner

            def __init__(self, inner: list[Track]) -> None:
                self.tracks = self.Tracks(inner)

        return Page(self.items)


def test_normalize_text_handles_case_accents_and_features() -> None:
    assert normalize_text("  Café  feat. Someone ") == "cafe"
    assert normalize_text("Ёлка — Потому что") == "елка потому что"


def test_local_duplicate_is_preferred() -> None:
    import asyncio

    original = _track(1, "Song A", "Artist")
    local = _track(2, "Song A (Remaster)", "Artist", local=True)
    yandex = _track(3, "Song A", "Artist", online=True)
    service = TrackFallbackService(FakeTracks([local]), FakeYandex([yandex]))

    found = asyncio.run(service.find(original, allow_online=True))

    assert found is local


def test_yandex_is_used_when_local_has_no_copy() -> None:
    import asyncio

    original = _track(1, "Song A", "Artist")
    yandex = _track(3, "Song A", "Artist", online=True)
    source = FakeYandex([yandex])
    service = TrackFallbackService(FakeTracks([]), source)

    found = asyncio.run(service.find(original, allow_online=True))

    assert found is yandex
    assert source.queries == ["Artist Song A"]


def test_wrong_artist_or_duration_is_rejected() -> None:
    import asyncio

    original = _track(1, "Song A", "Artist", duration_ms=200_000)
    wrong_artist = _track(2, "Song A", "Other", local=True)
    wrong_duration = _track(3, "Song A", "Artist", local=True, duration_ms=400_000)
    service = TrackFallbackService(
        FakeTracks([wrong_artist, wrong_duration]), FakeYandex([])
    )

    assert asyncio.run(service.find(original)) is None


def test_online_search_is_skipped_when_cdn_is_blocked() -> None:
    import asyncio

    source = FakeYandex([_track(2, "Song A", "Artist", online=True)])
    service = TrackFallbackService(FakeTracks([]), source)

    assert asyncio.run(service.find(_track(1, "Song A", "Artist"), allow_online=False)) is None
    assert source.queries == []
