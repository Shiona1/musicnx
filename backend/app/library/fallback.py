"""Automatic track fallback when the current source cannot be played.

Two legal providers are searched:

1. the local library — another copy of the same title/artist with a real file;
2. the Yandex catalog — a different track/version (not the failing track id).

Deliberately no YouTube/VK/SoundCloud ripping: MusicNX is a personal player and
does not bypass copyright/DRM. The provider interface is intentionally small so
a licensed external provider can be added later.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.core.models import Track
from app.database.repositories import TrackRepository
from app.logging_setup import get_logger

logger = get_logger("library.fallback")

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")
_FEAT = re.compile(r"\b(feat|ft|featuring|prod)\b.*$", re.IGNORECASE)
_DURATION_TOLERANCE_MS = 20_000


def normalize_text(value: str | None) -> str:
    """Lowercase, strip accents/punctuation and collapse whitespace."""

    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("ё", "е")
    text = _FEAT.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def _artist_names(track: Track) -> set[str]:
    return {normalize_text(artist.name) for artist in track.artists if artist.name}


def _same_title(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    # Remaster/live/radio-edit suffixes are common alternate versions.
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 4 and shorter in longer


def _matches(candidate: Track, original: Track) -> bool:
    title = normalize_text(candidate.title)
    if not _same_title(title, normalize_text(original.title)):
        return False
    original_artists = _artist_names(original)
    candidate_artists = _artist_names(candidate)
    if (
        original_artists
        and candidate_artists
        and not (original_artists & candidate_artists)
    ):
        return False
    return not (
        original.duration_ms
        and candidate.duration_ms
        and abs(original.duration_ms - candidate.duration_ms) > _DURATION_TOLERANCE_MS
    )


class TrackFallbackService:
    """Find a playable alternate version of a failed track."""

    def __init__(
        self,
        tracks: TrackRepository,
        yandex: Any | None = None,
    ) -> None:
        self._tracks = tracks
        self._yandex = yandex

    async def find(self, track: Track, *, allow_online: bool = True) -> Track | None:
        local = await self._find_local(track)
        if local is not None:
            logger.info(
                "track fallback: %s -> local %s",
                track.id,
                local.id,
            )
            return local
        if allow_online and self._yandex is not None:
            online = await self._find_yandex(track)
            if online is not None:
                logger.info(
                    "track fallback: %s -> yandex %s",
                    track.id,
                    online.id,
                )
                return online
        return None

    async def _find_local(self, track: Track) -> Track | None:
        try:
            candidates, _ = await self._tracks.list(
                limit=40, search=track.title, availability="all"
            )
        except Exception as exc:  # a fallback must never break playback handling
            logger.warning("local fallback search failed: %s", type(exc).__name__)
            return None
        for candidate in candidates:
            if candidate.id == track.id or not candidate.has_local_file:
                continue
            if _matches(candidate, track):
                return candidate
        return None

    async def _find_yandex(self, track: Track) -> Track | None:
        query = " ".join(
            part
            for part in (
                track.artists[0].name if track.artists else None,
                track.title,
            )
            if part
        ).strip()
        if not query:
            return None
        try:
            page = await self._yandex.search(query, type_="tracks", limit=10, offset=0)
        except Exception as exc:
            logger.warning("yandex fallback search failed: %s", type(exc).__name__)
            return None
        for candidate in page.tracks.items:
            if candidate.id == track.id:
                continue
            if not candidate.has_online_source:
                continue
            if _matches(candidate, track):
                return candidate
        return None


__all__ = ["TrackFallbackService", "normalize_text"]
