"""Write P4 tags and embedded cover with mutagen (blocking, call via ``to_thread``).

Containers follow docs/DOWNLOADS.md §5:

* MP3 -> ID3v2.4 (UTF-8);
* FLAC -> Vorbis Comments;
* M4A/MP4 -> MP4 ``ilst``.

``flac-mp4`` is stored as ``.m4a`` because that is the real container. Mutagen
may not understand FLAC-in-MP4; the writer then returns a warning instead of
raising, so the pipeline still keeps the factual file/format. A tag failure never
turns a successful download into a failed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    ID3NoHeaderError,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

_MP4_LABEL_KEY = "----:com.apple.iTunes:LABEL"

_ID3_KNOWN_FRAMES = ("TIT2", "TPE1", "TPE2", "TALB", "TDRC", "TCON", "TRCK", "TPOS", "TPUB")
_MP4_KNOWN_KEYS = (
    "\xa9nam",
    "\xa9ART",
    "aART",
    "\xa9alb",
    "\xa9day",
    "\xa9gen",
    "trkn",
    "disk",
    _MP4_LABEL_KEY,
)


@dataclass(frozen=True)
class TrackTags:
    """Metadata written to a downloaded file and returned to the manager."""

    title: str
    artists: tuple[str, ...] = ()
    album: str | None = None
    albumartist: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    label: str | None = None


@dataclass(frozen=True)
class TagWriteReport:
    """Outcome of a best-effort tag write."""

    container: str
    written: bool
    warning: str | None = None


def _artists(tags: TrackTags) -> tuple[str, ...]:
    cleaned = tuple(name.strip() for name in tags.artists if name and name.strip())
    return cleaned or ("Unknown Artist",)


def _albumartist(tags: TrackTags) -> str:
    if tags.albumartist and tags.albumartist.strip():
        return tags.albumartist.strip()
    return ", ".join(_artists(tags))


def tag_container(path: Path) -> str | None:
    """Return the tag container for a file suffix, or ``None`` if unsupported."""

    suffix = path.suffix.lower()
    if suffix == ".mp3":
        return "id3"
    if suffix == ".flac":
        return "vorbis"
    if suffix in {".m4a", ".mp4"}:
        return "mp4"
    return None


def write_tags(
    path: Path, tags: TrackTags, *, cover: bytes | None = None
) -> TagWriteReport:
    """Write tags (and an optional JPEG cover) without ever raising.

    The caller runs this in a worker thread. Returned warnings are logged by the
    pipeline; the audio bytes themselves are never modified or transcoded.
    """

    container = tag_container(path)
    if container is None:
        return TagWriteReport(
            container="unknown",
            written=False,
            warning=f"no tag writer for suffix {path.suffix!r}",
        )
    try:
        if container == "id3":
            _write_id3(path, tags, cover)
        elif container == "vorbis":
            _write_vorbis(path, tags, cover)
        else:
            _write_mp4(path, tags, cover)
    except Exception as exc:  # mutagen raises many format-specific errors
        message = str(exc).strip().replace("\n", " ")[:300]
        return TagWriteReport(
            container=container,
            written=False,
            warning=f"{type(exc).__name__}: {message}" if message else type(exc).__name__,
        )
    return TagWriteReport(container=container, written=True)


def _write_id3(path: Path, tags: TrackTags, cover: bytes | None) -> None:
    try:
        audio = ID3(path)
    except ID3NoHeaderError:
        audio = ID3()

    for frame in _ID3_KNOWN_FRAMES:
        audio.delall(frame)
    audio.add(TIT2(encoding=3, text=[tags.title]))
    audio.add(TPE1(encoding=3, text=list(_artists(tags))))
    audio.add(TPE2(encoding=3, text=[_albumartist(tags)]))
    if tags.album:
        audio.add(TALB(encoding=3, text=[tags.album]))
    if tags.year:
        audio.add(TDRC(encoding=3, text=[str(tags.year)]))
    if tags.genre:
        audio.add(TCON(encoding=3, text=[tags.genre]))
    if tags.track_number is not None:
        audio.add(TRCK(encoding=3, text=[str(tags.track_number)]))
    if tags.disc_number is not None:
        audio.add(TPOS(encoding=3, text=[str(tags.disc_number)]))
    if tags.label:
        audio.add(TPUB(encoding=3, text=[tags.label]))
    if cover:
        audio.delall("APIC")
        audio.add(
            APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover)
        )
    audio.save(path, v2_version=4)


def _write_vorbis(path: Path, tags: TrackTags, cover: bytes | None) -> None:
    audio = FLAC(path)
    audio["title"] = [tags.title]
    audio["artist"] = list(_artists(tags))
    audio["albumartist"] = [_albumartist(tags)]
    if tags.album:
        audio["album"] = [tags.album]
    if tags.year:
        audio["date"] = [str(tags.year)]
    if tags.genre:
        audio["genre"] = [tags.genre]
    if tags.track_number is not None:
        audio["tracknumber"] = [str(tags.track_number)]
    if tags.disc_number is not None:
        audio["discnumber"] = [str(tags.disc_number)]
    if tags.label:
        audio["label"] = [tags.label]
    if cover:
        audio.clear_pictures()
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = "image/jpeg"
        picture.desc = "Cover"
        picture.data = cover
        audio.add_picture(picture)
    audio.save()


def _write_mp4(path: Path, tags: TrackTags, cover: bytes | None) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    mp4_tags = audio.tags
    if mp4_tags is None:  # pragma: no cover - mutagen always creates tags above
        raise ValueError("MP4 tags could not be created")

    for key in _MP4_KNOWN_KEYS:
        mp4_tags.pop(key, None)
    mp4_tags["\xa9nam"] = [tags.title]
    mp4_tags["\xa9ART"] = list(_artists(tags))
    mp4_tags["aART"] = [_albumartist(tags)]
    if tags.album:
        mp4_tags["\xa9alb"] = [tags.album]
    if tags.year:
        mp4_tags["\xa9day"] = [str(tags.year)]
    if tags.genre:
        mp4_tags["\xa9gen"] = [tags.genre]
    if tags.track_number is not None:
        mp4_tags["trkn"] = [(tags.track_number, 0)]
    if tags.disc_number is not None:
        mp4_tags["disk"] = [(tags.disc_number, 0)]
    if tags.label:
        mp4_tags[_MP4_LABEL_KEY] = [MP4FreeForm(tags.label.encode("utf-8"))]
    if cover:
        mp4_tags["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
