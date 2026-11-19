"""P4 source selection: honest quality fallback and factual container mapping.

Yandex Music offers several already-encoded versions of a track. MusicNX never
transcodes (AGENTS.md §9); it only *picks* one of them. This module describes
what is actually on the wire (codec, transport, preview flag) and what file
extension the pipeline must use on disk:

* ``flac`` -> ``.flac``;
* ``flac-mp4`` / ``aac-mp4`` / ``he-aac-mp4`` -> ``.m4a`` (MP4 container);
* ``mp3`` -> ``.mp3``.

``FileDownloadInfo.quality`` uses ``lossless``/``hq``/``nq`` labels; the codec
is authoritative when both are present, so a mismatched response degrades to
the codec family instead of claiming a quality that was not delivered.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.downloads.base import SourceFile

DEFAULT_QUALITY_ORDER: tuple[str, ...] = ("flac", "aac", "mp3")
QUALITY_FAMILIES = frozenset(DEFAULT_QUALITY_ORDER)

_SAFE_CODEC = re.compile(r"[^a-z0-9]+")

# ``FileDownloadInfo.quality`` labels -> configured quality family.
_QUALITY_ALIASES: dict[str, str] = {
    "lossless": "flac",
    "flac": "flac",
    "hq": "aac",
    "aac": "aac",
    "nq": "mp3",
    "mp3": "mp3",
}

# codec -> (quality family, on-disk format, extension, expected magic container).
_CODEC_MAP: dict[str, tuple[str, str, str, str | None]] = {
    "flac": ("flac", "flac", ".flac", "flac"),
    "flac-mp4": ("flac", "m4a", ".m4a", "mp4"),
    "alac-mp4": ("flac", "m4a", ".m4a", "mp4"),
    "aac-mp4": ("aac", "m4a", ".m4a", "mp4"),
    "he-aac": ("aac", "aac", ".aac", "aac"),
    "he-aac-mp4": ("aac", "m4a", ".m4a", "mp4"),
    "aac": ("aac", "aac", ".aac", "aac"),
    "m4a": ("aac", "m4a", ".m4a", "mp4"),
    "mp3": ("mp3", "mp3", ".mp3", "mpeg"),
    "mp3-mp4": ("mp3", "m4a", ".m4a", "mp4"),
}


class NoSourceAvailableError(ValueError):
    """The source returned no usable file for the requested quality order."""


@dataclass(frozen=True)
class CodecInfo:
    """Factual codec data: never the promised quality, always the delivered one."""

    raw: str
    family: str | None
    format: str
    extension: str
    container: str | None


@dataclass(frozen=True)
class SourceSelection:
    """One resolved source with its factual representation."""

    source: SourceFile
    codec: CodecInfo
    requested_quality: str
    actual_quality: str
    transport: str
    preview: bool
    fell_back: bool


def _clean(value: str | None) -> str:
    return (value or "").strip().lower()


def classify_codec(codec: str | None) -> CodecInfo:
    """Map a Yandex codec name to its factual container and extension."""

    raw = _clean(codec) or "unknown"
    mapped = _CODEC_MAP.get(raw)
    if mapped is not None:
        family, on_disk_format, extension, container = mapped
        return CodecInfo(
            raw=raw,
            family=family,
            format=on_disk_format,
            extension=extension,
            container=container,
        )
    # Unknown codecs are copied verbatim (no transcoding): keep a safe suffix
    # so the file is still usable/identifiable, but do not claim a known magic.
    safe = _SAFE_CODEC.sub("", raw)[:16] or "bin"
    return CodecInfo(
        raw=raw,
        family=None,
        format=safe,
        extension=f".{safe}",
        container=None,
    )


def quality_family(value: str | None) -> str | None:
    """Normalise ``lossless``/``hq``/``nq`` or a codec name to a family."""

    cleaned = _clean(value)
    if cleaned in _QUALITY_ALIASES:
        return _QUALITY_ALIASES[cleaned]
    mapped = _CODEC_MAP.get(cleaned)
    return mapped[0] if mapped is not None else None


def normalize_transport(transport: str | None) -> str:
    """Only ``encraw`` triggers decryption; anything else is treated as raw."""

    return "encraw" if _clean(transport) == "encraw" else "raw"


def normalize_quality_order(order: Sequence[str] | None) -> tuple[str, ...]:
    """Keep known families in the configured order, de-duplicated."""

    result: list[str] = []
    for item in order or ():
        family = quality_family(item)
        if family is not None and family not in result:
            result.append(family)
    return tuple(result) or DEFAULT_QUALITY_ORDER


def quality_rank(family: str | None, order: Sequence[str]) -> int:
    """Lower rank is preferred; unknown families go last."""

    if family is None:
        return len(order) + 1
    try:
        return list(order).index(family)
    except ValueError:
        return len(order)


def source_quality(source: SourceFile) -> str | None:
    """Factual family: the codec wins over a possibly stale ``quality`` label."""

    return classify_codec(source.codec).family or quality_family(source.quality)


def extension_for_source(source: SourceFile) -> str:
    return classify_codec(source.codec).extension


def format_for_source(source: SourceFile) -> str:
    return classify_codec(source.codec).format


def describe_source(
    source: SourceFile, quality_order: Sequence[str] | None = None
) -> SourceSelection:
    """Describe one already-resolved source; ``fell_back`` is purely factual."""

    order = normalize_quality_order(quality_order)
    codec = classify_codec(source.codec)
    actual = source_quality(source) or _clean(source.quality) or "unknown"
    requested = order[0]
    return SourceSelection(
        source=source,
        codec=codec,
        requested_quality=requested,
        actual_quality=actual,
        transport=normalize_transport(source.transport),
        preview=bool(source.preview),
        fell_back=actual != requested,
    )


def select_source_file(
    candidates: Sequence[SourceFile], quality_order: Sequence[str] | None = None
) -> SourceSelection:
    """Pick the best full track from candidates without transcoding.

    The configured order is honoured first. Preview files are used only when no
    full file exists: saving a short preview while calling it the requested
    quality would be dishonest. Within one family the original order wins (the
    adapter already sorts by bitrate), so selection stays deterministic.
    """

    usable = [
        source for source in candidates if source is not None and _clean(source.url)
    ]
    if not usable:
        raise NoSourceAvailableError("source returned no downloadable file")

    order = normalize_quality_order(quality_order)
    full = [source for source in usable if not source.preview]
    pool = full or usable

    def key(indexed: tuple[int, SourceFile]) -> tuple[int, int]:
        index, source = indexed
        return quality_rank(source_quality(source), order), index

    best = min(enumerate(pool), key=key)[1]
    return describe_source(best, order)
