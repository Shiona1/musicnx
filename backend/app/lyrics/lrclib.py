"""LRCLIB fallback provider: community lyrics when local/Yandex have none.

LRCLIB (https://lrclib.net) exposes a free JSON API. We send only track
metadata (artist, title, album, duration) and never log the returned lyrics.
The lookup is bounded by ``lyrics.lrclib_timeout_s`` and degrades to ``None``
on any network/HTTP error, so a lyrics request never turns into a 500.
"""

from __future__ import annotations

import aiohttp

from app.logging_setup import get_logger

logger = get_logger("lyrics.lrclib")

BASE_URL = "https://lrclib.net/api"
USER_AGENT = "MusicNX/0.2.0 (self-hosted lyrics fallback)"


def _usable(document: object) -> dict[str, str] | None:
    if not isinstance(document, dict):
        return None
    if bool(document.get("instrumental")):
        return None
    synced = document.get("syncedLyrics")
    plain = document.get("plainLyrics")
    result: dict[str, str] = {}
    if isinstance(synced, str) and synced.strip():
        result["syncedLyrics"] = synced
    elif isinstance(plain, str) and plain.strip():
        result["plainLyrics"] = plain
    return result or None


async def lookup(
    artist: str,
    title: str,
    album: str | None,
    duration_s: int | None,
    *,
    timeout_s: float = 6.0,
) -> dict[str, str] | None:
    """Return ``{"syncedLyrics"|"plainLyrics": ...}`` or ``None``."""

    if not artist.strip() or not title.strip():
        return None
    params: dict[str, str | int] = {
        "artist_name": artist.strip(),
        "track_name": title.strip(),
    }
    if album and album.strip():
        params["album_name"] = album.strip()
    if duration_s and duration_s > 0:
        params["duration"] = duration_s

    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=timeout_s)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(f"{BASE_URL}/get", params=params) as response:
                if response.status == 200:
                    return _usable(await response.json(content_type=None))
                if response.status != 404:
                    logger.warning("lrclib get failed: HTTP %s", response.status)
                    return None

            # Exact lookup missed (common for feat./remaster variants): try a
            # free-text search and accept the first document that has lyrics.
            async with session.get(
                f"{BASE_URL}/search",
                params={"q": f"{artist} {title}", "track_name": title, "artist_name": artist},
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    for item in payload[:5]:
        usable = _usable(item)
        if usable is not None:
            return usable
    return None


__all__ = ["lookup"]
