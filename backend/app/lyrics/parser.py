"""Pure LRC/plain lyrics parser (no network, no database, no I/O).

The parser is deliberately forgiving: real-world ``.lrc`` files mix timestamp
precision (``[mm:ss]``, ``[mm:ss.xx]``, ``[mm:ss.xxx]``), put several timestamps
on one line, carry ID tags (``[ar:]``, ``[ti:]``, ``[al:]``) and a global
``[offset:]``. Anything that cannot be interpreted becomes either metadata
(ignored) or an untimed line (ignored for LRC, preserved for plain text).

Offset convention: ``[offset:+/-ms]`` is *already applied* to every timestamp
in the returned lines (positive value delays the lyrics, negative makes them
appear earlier) and the final timestamps are clamped at zero.  The reported
``ParsedLyrics.offset_ms`` is the raw tag value, kept for diagnostics only —
callers must not apply it a second time.  Per-user offsets live in the
repository and are applied with :func:`apply_offset`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.core.models import LyricLine

LyricsFormat = Literal["lrc", "plain"]

_BOM = "\ufeff"

# ``[-]mm:ss``, optionally ``.xx`` / ``.xxx`` (some files use ``:xx`` instead).
_TIMESTAMP_TAG = re.compile(r"\[(-?\d{1,4}:[0-5]?\d(?:[.:]\d{1,3})?)\]")
_TIMESTAMP_PARTS = re.compile(r"(-?)(\d{1,4}):([0-5]?\d)(?:[.:](\d{1,3}))?")

# One or more timestamp tags at the start of a line, then the line text.
_LEADING_TIMESTAMPS = re.compile(
    r"^\s*((?:\[-?\d{1,4}:[0-5]?\d(?:[.:]\d{1,3})?\])+)(.*)$"
)

# A whole line that is a single ``[key:value]`` tag (ID tags, offset, etc.).
_METADATA_LINE = re.compile(r"^\s*\[([A-Za-z]+):([^\]]*)\]\s*$")

# Metadata keys that never become lyric lines.  ``offset`` is the only one
# with semantics; the rest are accepted so they are not mistaken for lyrics.
_METADATA_KEYS = frozenset({"ar", "ti", "al", "by", "re", "ve", "length", "offset"})


@dataclass(frozen=True)
class ParsedLyrics:
    """Normalised lyrics ready for caching or for the UI.

    ``plain`` is only set for ``format="plain"``; ``lines`` is only populated
    for ``format="lrc"``.  ``offset_ms`` is the raw ``[offset:]`` tag from the
    file (already folded into ``lines``; informational only).  ``language`` is
    ``"und"`` here because the parser only sees text — the caller knows the
    language and passes it to the repository.
    """

    format: LyricsFormat
    lines: list[LyricLine]
    plain: str | None = None
    language: str = "und"
    offset_ms: int = 0


def _normalise_newlines(text: str) -> str:
    if text.startswith(_BOM):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fraction_ms(fraction: str | None) -> int:
    """``.5`` -> 500 ms, ``.25`` -> 250 ms, ``.125`` -> 125 ms."""

    if not fraction:
        return 0
    return int(fraction.ljust(3, "0")[:3])


def _tag_to_ms(tag: str) -> int:
    """Convert one timestamp tag body (``mm:ss[.fff]``) to milliseconds."""

    match = _TIMESTAMP_PARTS.fullmatch(tag)
    if match is None:  # pragma: no cover - the outer regex guarantees the shape
        return 0
    sign, minutes, seconds, fraction = match.groups()
    total = int(minutes) * 60_000 + int(seconds) * 1_000 + _fraction_ms(fraction)
    if sign == "-":
        total = -total
    # Negative/garbage timestamps must never reach the UI as negative values.
    return max(0, total)


def _offset_value(raw: str) -> int | None:
    """Parse the numeric part of ``[offset:+/-ms]``; ``None`` if malformed."""

    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_lrc(text: str) -> ParsedLyrics:
    """Parse LRC text into time-sorted lines.

    Behaviour:
      * several timestamp tags on one line produce one line per timestamp;
      * ``[offset:+/-ms]`` shifts every timestamp (already applied here, never
        apply it again) and negative results are clamped to zero;
      * exact ``(time_ms, text)`` duplicates are collapsed (first wins),
        while the same timestamp with different text is kept;
      * timed empty lines are preserved (instrumental gaps);
      * untimed text lines and ID tags are not lyric lines;
      * text is preserved verbatim (Unicode included), no trimming;
      * timestamps are sorted ascending and equal timestamps keep file order.
    """

    normalised = _normalise_newlines(text)
    offset_ms = 0
    entries: list[tuple[int, str]] = []

    for raw_line in normalised.split("\n"):
        metadata = _METADATA_LINE.match(raw_line)
        if metadata is not None and metadata.group(1).lower() in _METADATA_KEYS:
            key = metadata.group(1).lower()
            if key == "offset":
                parsed_offset = _offset_value(metadata.group(2))
                if parsed_offset is not None:
                    # The last offset tag wins, matching common LRC players.
                    offset_ms = parsed_offset
            continue

        match = _LEADING_TIMESTAMPS.match(raw_line)
        if match is None:
            continue
        tags, lyric_text = match.groups()
        for tag_match in _TIMESTAMP_TAG.finditer(tags):
            entries.append((_tag_to_ms(tag_match.group(1)), lyric_text))

    shifted = [(max(0, time_ms + offset_ms), lyric_text) for time_ms, lyric_text in entries]
    shifted.sort(key=lambda entry: entry[0])

    lines: list[LyricLine] = []
    seen: set[tuple[int, str]] = set()
    for time_ms, lyric_text in shifted:
        entry = (time_ms, lyric_text)
        if entry in seen:
            continue
        seen.add(entry)
        lines.append(LyricLine(time_ms=time_ms, text=lyric_text))
    return ParsedLyrics(format="lrc", lines=lines, plain=None, offset_ms=offset_ms)


def normalize_plain(text: str) -> str:
    """Normalise plain lyrics: BOM, CRLF/CR, trailing spaces, outer blank lines."""

    normalised = _normalise_newlines(text)
    normalised = "\n".join(line.rstrip() for line in normalised.split("\n"))
    return normalised.strip("\n")


def parse_plain(text: str) -> ParsedLyrics:
    """Wrap untimed text as plain lyrics (no lines, normalised ``plain``)."""

    return ParsedLyrics(format="plain", lines=[], plain=normalize_plain(text))


def _looks_like_lrc(text: str) -> bool:
    normalised = _normalise_newlines(text)
    for raw_line in normalised.split("\n"):
        if _LEADING_TIMESTAMPS.match(raw_line) is not None:
            return True
    return False


def parse(text: str, format_hint: LyricsFormat | None = None) -> ParsedLyrics:
    """Parse with an optional authoritative hint, else sniff the format.

    An explicit ``format_hint`` is honoured as-is (the caller knows the
    origin); without a hint, text containing at least one timestamp is parsed
    as LRC, everything else falls back to plain lyrics.
    """

    hint = format_hint.lower() if format_hint is not None else None
    if hint == "lrc":
        return parse_lrc(text)
    if hint == "plain":
        return parse_plain(text)
    if _looks_like_lrc(text):
        return parse_lrc(text)
    return parse_plain(text)


def apply_offset(lines: Sequence[LyricLine], offset_ms: int) -> list[LyricLine]:
    """Return lines shifted by ``offset_ms`` (positive = later), clamped at 0.

    The input list is not mutated; texts are copied verbatim.
    """

    return [
        LyricLine(time_ms=max(0, line.time_ms + offset_ms), text=line.text)
        for line in lines
    ]


def line_at(lines: Sequence[LyricLine], position_ms: int) -> int | None:
    """Index of the last line whose ``time_ms <= position_ms``.

    Returns ``None`` before the first line (or for empty lyrics), which the UI
    renders as "no highlighted line yet".  ``lines`` is expected in ascending
    time order as produced by :func:`parse_lrc`.
    """

    found: int | None = None
    for index, line in enumerate(lines):
        if line.time_ms <= position_ms:
            found = index
    return found


__all__ = [
    "LyricsFormat",
    "ParsedLyrics",
    "apply_offset",
    "line_at",
    "normalize_plain",
    "parse",
    "parse_lrc",
    "parse_plain",
]
