"""Lyrics domain: pure LRC parsing plus the SQLite cache.

``parser`` never touches the network or the database; ``repository`` only
knows the frozen ``lyrics`` table.  Import through this package to keep the
public surface stable for the service/API layer.
"""

from __future__ import annotations

from app.lyrics.parser import (
    LyricsFormat,
    ParsedLyrics,
    apply_offset,
    line_at,
    normalize_plain,
    parse,
    parse_lrc,
    parse_plain,
)
from app.lyrics.repository import (
    LYRICS_FORMATS,
    OFFSET_LIMIT_MS,
    LyricsRecord,
    LyricsRepository,
    clamp_offset,
    normalize_language,
)

__all__ = [
    "LYRICS_FORMATS",
    "OFFSET_LIMIT_MS",
    "LyricsFormat",
    "LyricsRecord",
    "LyricsRepository",
    "ParsedLyrics",
    "apply_offset",
    "clamp_offset",
    "line_at",
    "normalize_language",
    "normalize_plain",
    "parse",
    "parse_lrc",
    "parse_plain",
]
