"""AES-128-CTR decryption for Yandex ``encraw`` files (docs/YANDEX_API.md §3).

``encraw`` files are AES-128-CTR encrypted with an all-zero counter. Two helpers
cover the two call sites:

* :func:`decrypt_bytes` wraps the confirmed library helper
  ``yandex_music.utils.decrypt.decrypt_track`` for small/in-memory payloads;
* :class:`AesCtrDecryptor` streams the same cipher via ``cryptography`` so the
  pipeline never has to buffer a whole track.

Keys and signed URLs are secrets: this module does not log anything, and
exception messages never include the key or the URL.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from yandex_music.exceptions import YandexMusicError
from yandex_music.utils.decrypt import decrypt_track

_HEX_KEY = 32  # 16 bytes = 32 hex characters


class DecryptError(ValueError):
    """``encraw`` payload or key cannot be decrypted."""


class MissingKeyError(DecryptError):
    """``encraw`` source has no usable key."""


def _key_bytes(key: str | bytes | None) -> bytes:
    """Validate a key and return its raw bytes without echoing it anywhere."""

    if key is None:
        raise MissingKeyError("encraw source is missing a key")
    if isinstance(key, bytes):
        raw = key
    elif isinstance(key, str):
        candidate = key.strip()
        if not candidate:
            raise MissingKeyError("encraw source is missing a key")
        try:
            raw = bytes.fromhex(candidate)
        except ValueError as exc:
            raise MissingKeyError("encraw key is not valid hex") from exc
    else:
        raise MissingKeyError("encraw key must be a hex string")
    if len(raw) != _HEX_KEY // 2:
        raise MissingKeyError("encraw key must be 16 bytes (32 hex characters)")
    return raw


def validate_key(key: str | bytes | None) -> bool:
    """Return whether ``key`` is usable; never raises and never logs."""

    try:
        _key_bytes(key)
    except DecryptError:
        return False
    return True


def decrypt_bytes(data: bytes, key: str | bytes) -> bytes:
    """Decrypt a complete ``encraw`` payload via the confirmed library helper."""

    _key_bytes(key)
    hex_key = key.hex() if isinstance(key, bytes) else key
    try:
        return decrypt_track(data, hex_key)
    except YandexMusicError as exc:
        raise DecryptError("encraw decryption failed") from exc
    except (TypeError, ValueError) as exc:
        raise DecryptError("encraw decryption failed") from exc


class AesCtrDecryptor:
    """Incremental AES-128-CTR decryptor with a zero counter (stream friendly).

    ``offset`` is the byte position of the first ciphertext byte in the original
    file. It is required when a download resumes from a ``.part`` file: the
    keystream must continue at the same offset, not restart at counter zero.
    """

    def __init__(self, key: str | bytes, *, offset: int = 0) -> None:
        if offset < 0:
            raise DecryptError("decryption offset must not be negative")
        block, within = divmod(offset, 16)
        try:
            counter = block.to_bytes(16, "big")
        except OverflowError as exc:  # pragma: no cover - impossible file size
            raise DecryptError("decryption offset is out of range") from exc
        self._decryptor = Cipher(
            algorithms.AES(_key_bytes(key)), modes.CTR(counter)
        ).decryptor()
        if within:
            # Discard the leading bytes of the block so the next ``update``
            # starts exactly at ``offset``.
            self._decryptor.update(bytes(within))
        self._finalized = False

    def update(self, data: bytes) -> bytes:
        if self._finalized:
            raise DecryptError("decryptor is already finalized")
        if not data:
            return b""
        return self._decryptor.update(data)

    def finalize(self) -> bytes:
        if self._finalized:
            return b""
        self._finalized = True
        return self._decryptor.finalize()


# Explicit alias for callers that name the streaming behaviour.
StreamingDecryptor = AesCtrDecryptor
