"""File integrity helpers: size, magic sniff and SHA-256 (P4).

The pipeline uses these after the atomic rename and after tags/cover are
written, so the checksum it returns describes the final playable file. All
functions are blocking by design and must run via ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHUNK_BYTES = 1_048_576
_HEADER_BYTES = 16

FLAC = "flac"
MP4 = "mp4"
MPEG = "mpeg"
AAC = "aac"
WAV = "wav"
OGG = "ogg"
UNKNOWN = "unknown"


class IntegrityError(ValueError):
    """File is missing, truncated or does not match the expected container."""


@dataclass(frozen=True)
class IntegrityReport:
    """Verified facts about a finished file."""

    path: Path
    size: int
    sha256: str
    container: str
    magic: str


def sniff_bytes(header: bytes) -> str:
    """Identify a container from its first bytes (pure, easy to test)."""

    if header[:4] == b"fLaC":
        return FLAC
    if len(header) >= 8 and header[4:8] in {b"ftyp", b"moov", b"mdat", b"free"}:
        return MP4
    if header[:3] == b"ID3":
        # ID3 is a tag header; the audio stream behind it is MPEG audio.
        return MPEG
    if len(header) >= 2 and header[0] == 0xFF:
        # ADTS sync is 12 bits (0xFFF); MPEG audio layer bits are non-zero.
        if (header[1] & 0xF6) == 0xF0:
            return AAC
        if (header[1] & 0xE0) == 0xE0:
            return MPEG
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return WAV
    if header[:4] == b"OggS":
        return OGG
    return UNKNOWN


def sniff_container(path: Path) -> str:
    """Read only the header; raises :class:`IntegrityError` for unreadable files."""

    try:
        with path.open("rb") as handle:
            header = handle.read(_HEADER_BYTES)
    except OSError as exc:
        raise IntegrityError(f"cannot read file: {path.name}") from exc
    return sniff_bytes(header)


def sha256_file(path: Path, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> str:
    """Stream a file through SHA-256 without loading it into memory."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(max(4096, chunk_bytes)):
                digest.update(chunk)
    except OSError as exc:
        raise IntegrityError(f"cannot hash file: {path.name}") from exc
    return digest.hexdigest()


def verify_file(
    path: Path,
    *,
    expected_size: int | None = None,
    expected_container: str | None = None,
    expected_sha256: str | None = None,
) -> IntegrityReport:
    """Check size, magic and checksum and return the verified facts."""

    try:
        stat = path.stat()
    except OSError as exc:
        raise IntegrityError(f"file is missing: {path.name}") from exc
    if not path.is_file():
        raise IntegrityError(f"not a regular file: {path.name}")
    if expected_size is not None and stat.st_size != expected_size:
        raise IntegrityError(
            f"size mismatch for {path.name}: expected {expected_size}, got {stat.st_size}"
        )

    container = sniff_container(path)
    if expected_container is not None and container != expected_container:
        raise IntegrityError(
            f"container mismatch for {path.name}: "
            f"expected {expected_container}, got {container}"
        )

    checksum = sha256_file(path)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise IntegrityError(f"sha256 mismatch for {path.name}")

    try:
        with path.open("rb") as handle:
            magic = handle.read(8).hex()
    except OSError as exc:  # pragma: no cover - the file was readable above
        raise IntegrityError(f"cannot read file: {path.name}") from exc

    return IntegrityReport(
        path=path.resolve(),
        size=stat.st_size,
        sha256=checksum,
        container=container,
        magic=magic,
    )
