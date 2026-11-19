"""Local media probing with mutagen (runs in a worker thread, never in the loop)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4, MP4Cover

from app.core.models import LocalMediaMetadata
from app.logging_setup import get_logger

logger = get_logger("library.probe")

SKIPPED_DIRS = {".git", ".svn", "@eaDir", "#recycle", "lost+found", "__pycache__"}


class MediaProbeError(ValueError):
    """File is not readable audio (corrupt/unsupported)."""


@dataclass(frozen=True)
class ProbeResult:
    metadata: LocalMediaMetadata
    cover: bytes | None = None


def _first(tags, *keys: str) -> str | None:
    if tags is None:
        return None
    for key in keys:
        try:
            value = tags.get(key)
        except (KeyError, TypeError, ValueError):
            continue
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                continue
            value = value[0]
        text = str(value).strip()
        if text:
            return text
    return None


def _all(tags, *keys: str) -> list[str]:
    values: list[str] = []
    if tags is None:
        return values
    for key in keys:
        try:
            raw = tags.get(key)
        except (KeyError, TypeError, ValueError):
            continue
        if raw is None:
            continue
        if not isinstance(raw, list):
            raw = [raw]
        for item in raw:
            text = str(item).strip()
            if text:
                values.append(text)
    return values


def _parse_number(value: str | None) -> int | None:
    if not value:
        return None
    first = value.replace("/", " ").split()[0].strip()
    try:
        return int(first)
    except (TypeError, ValueError):
        return None


def _parse_year(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(ch for ch in value[:4] if ch.isdigit())
    if len(digits) == 4:
        return int(digits)
    return None


def _duration_ms(audio) -> int | None:
    try:
        length = float(audio.info.length)
    except (AttributeError, TypeError, ValueError):
        return None
    if length <= 0:
        return None
    return int(round(length * 1000))


def _extract_cover(audio) -> bytes | None:
    try:
        if isinstance(audio, FLAC) and audio.pictures:
            return audio.pictures[0].data
        tags = getattr(audio, "tags", None)
        if isinstance(tags, ID3):
            frames = tags.getall("APIC")
            if frames:
                return frames[0].data
        if isinstance(audio, MP4) and audio.tags is not None:
            covers = audio.tags.get("covr")
            if covers:
                cover = covers[0]
                if isinstance(cover, MP4Cover):
                    return bytes(cover)
        if tags is not None and hasattr(tags, "getall"):
            frames = tags.getall("APIC")
            if frames:
                return frames[0].data
    except (KeyError, IndexError, OSError, ValueError, TypeError):
        logger.debug("cannot extract cover from %s", getattr(audio, "filename", "?"))
    return None


def probe_media(path: Path, root: Path) -> ProbeResult:
    """Read tags and embedded cover. Must be called via ``asyncio.to_thread``."""

    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:  # mutagen raises many format-specific errors
        raise MediaProbeError(f"cannot read {path.name}: {exc}") from exc
    if audio is None:
        raise MediaProbeError(f"unsupported audio file: {path.name}")

    tags = getattr(audio, "tags", None)
    artists = _all(tags, "artist", "artists")
    # Some taggers put "A; B" or "A / B" into a single value.
    split_artists: list[str] = []
    for artist in artists:
        for part in artist.replace(";", "/").split("/"):
            name = part.strip()
            if name and name not in split_artists:
                split_artists.append(name)
    if not split_artists:
        split_artists = ["Unknown Artist"]

    title = _first(tags, "title") or path.stem
    album = _first(tags, "album")
    albumartist = _first(tags, "albumartist")
    year = _parse_year(_first(tags, "date", "year", "originaldate"))
    genre = _first(tags, "genre")
    label = _first(tags, "label", "organization", "publisher")
    track_number = _parse_number(_first(tags, "tracknumber", "track"))
    disc_number = _parse_number(_first(tags, "discnumber", "disc"))
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:  # pragma: no cover - caller validates roots
        raise MediaProbeError(f"{path} is outside import root") from exc

    info = getattr(audio, "info", None)

    def _info(name: str) -> int | None:
        value = getattr(info, name, None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    metadata = LocalMediaMetadata(
        path=path.resolve(),
        relative_path=relative,
        format=path.suffix.lower().lstrip("."),
        title=title,
        artists=split_artists,
        album=album,
        albumartist=albumartist,
        year=year,
        genre=genre,
        track_number=track_number,
        disc_number=disc_number,
        label=label,
        duration_ms=_duration_ms(audio),
        size_bytes=path.stat().st_size,
        bitrate=_info("bitrate"),
        sample_rate=_info("sample_rate"),
        channels=_info("channels"),
    )

    cover: bytes | None = None
    try:
        full = mutagen.File(path, easy=False)
        if full is not None:
            cover = _extract_cover(full)
    except Exception as exc:  # cover is optional; never fail the import for it
        logger.debug("cover extraction failed for %s: %s", path, exc)
    return ProbeResult(metadata=metadata, cover=cover)


def discover_media_files(roots: list[Path], extensions: list[str]) -> list[Path]:
    """Walk roots once, skip hidden/system dirs and symlinks escaping roots."""

    allowed = {suffix.lower() for suffix in extensions}
    resolved_roots = [root.resolve() for root in roots]
    found: list[Path] = []
    for root in resolved_roots:
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".") and name not in SKIPPED_DIRS
            ]
            current_path = Path(current)
            for name in files:
                if name.startswith("."):
                    continue
                candidate = current_path / name
                if candidate.suffix.lower() not in allowed:
                    continue
                try:
                    resolved = candidate.resolve()
                    if not any(resolved.is_relative_to(item) for item in resolved_roots):
                        continue
                    if resolved.is_file():
                        found.append(resolved)
                except OSError:
                    continue
    return sorted(set(found))
