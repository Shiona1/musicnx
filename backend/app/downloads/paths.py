"""Download path template (pure functions, tested in P1, used by P4).

Template placeholders: ``artists``, ``album``, ``year``, ``disc``, ``track``,
``title``, ``ext``. Every path component is sanitised, and the rendered path is
guaranteed to stay a relative path inside the downloads root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH_TEMPLATE = "{artists}/{album} ({year})/Disc {disc}/{track:02d} - {title}{ext}"

MAX_COMPONENT_LENGTH = 120
_ILLEGAL_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTIPLE_SPACES = re.compile(r"\s{2,}")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class PathTemplateError(ValueError):
    """Template or context cannot produce a safe relative path."""


@dataclass(frozen=True)
class PathContext:
    """Metadata available when laying out a downloaded track."""

    artists: str
    album: str | None
    year: int | None
    disc: int | None
    track: int | None
    title: str
    ext: str

    @classmethod
    def from_metadata(
        cls,
        *,
        title: str,
        artists: str,
        album: str | None = None,
        year: int | None = None,
        disc: int | None = None,
        track: int | None = None,
        ext: str = "",
    ) -> PathContext:
        return cls(
            artists=artists,
            album=album,
            year=year,
            disc=disc,
            track=track,
            title=title,
            ext=ext,
        )


def _normalize_extension(ext: str) -> str:
    cleaned = ext.strip().lstrip(".").lower()
    cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
    if not cleaned:
        return ""
    return f".{cleaned[:8]}"


def _truncate(value: str, limit: int = MAX_COMPONENT_LENGTH) -> str:
    if len(value) <= limit:
        return value
    suffix = ""
    dot = value.rfind(".")
    if dot > 0 and len(value) - dot <= 16:
        suffix = value[dot:]
        base = value[:dot]
    else:
        base = value
    keep = max(limit - len(suffix), 1)
    return base[:keep].rstrip(" .") + suffix


def sanitize_component(value: str, *, fallback: str = "Unknown") -> str:
    """Make one path component safe across Linux and Windows."""

    cleaned = _ILLEGAL_CHARACTERS.sub("_", value)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = _MULTIPLE_SPACES.sub(" ", cleaned).strip(" .")
    cleaned = _clean_empty_parens(cleaned)
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}_"
    return _truncate(cleaned) or fallback


def _clean_empty_parens(value: str) -> str:
    previous = None
    current = value
    while previous != current:
        previous = current
        current = current.replace("()", "").replace("( )", "")
        current = _MULTIPLE_SPACES.sub(" ", current).strip()
    return current


def render_relative_path(template: str, context: PathContext) -> Path:
    """Render and sanitise a relative path from ``template``.

    The template is split on ``/`` *before* substitution, so a slash inside a
    title cannot create a new directory level.
    """

    if not template or not template.strip():
        raise PathTemplateError("path template must not be empty")

    raw_segments = template.split("/")
    if any(segment in {"", ".", ".."} for segment in raw_segments):
        raise PathTemplateError("path template must not contain empty, '.' or '..' segments")

    values: dict[str, str | int] = {
        "artists": context.artists or "Unknown Artist",
        "album": context.album or "Unknown Album",
        "year": context.year if context.year else "",
        "disc": context.disc if context.disc is not None else 1,
        "track": context.track if context.track is not None else 0,
        "title": context.title or "Untitled",
        "ext": _normalize_extension(context.ext),
    }

    segments: list[str] = []
    for segment in raw_segments:
        try:
            rendered = segment.format_map(values)
        except (KeyError, IndexError, ValueError) as exc:
            raise PathTemplateError(f"invalid path template segment {segment!r}: {exc}") from exc
        cleaned = sanitize_component(rendered)
        segments.append(cleaned)

    path = Path(*segments)
    if path.is_absolute() or path.drive:
        raise PathTemplateError("rendered path must be relative")
    return path


def ensure_within_root(root: Path, relative: Path) -> Path:
    """Resolve ``relative`` under ``root`` and reject traversal."""

    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise PathTemplateError(f"path escapes downloads root: {relative}")
    return resolved
