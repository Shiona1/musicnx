"""Virtual audio backend used in mock mode and tests.

Position is derived from ``time.monotonic`` while "playing", so tests observe
real progress without decoding audio. There is no sound output; the UI shows a
MOCK badge.
"""

from __future__ import annotations

import time
from typing import Any

from app.audio.base import AudioBackend


class MockAudioBackend(AudioBackend):
    name = "mock"

    def __init__(self) -> None:
        self._volume = 70
        self._muted = False
        self._duration_ms = 0
        self._base_ms = 0
        self._position_ms = 0
        self._playing = False
        self._started_at: float | None = None
        self._eof = False
        self._error: str | None = None
        self._uri: str | None = None

    @property
    def ready(self) -> bool:
        return True

    @property
    def uri(self) -> str | None:
        return self._uri

    async def start(self) -> None:
        self._error = None

    async def shutdown(self) -> None:
        self._playing = False

    async def load(
        self, uri: str, *, start_ms: int = 0, duration_ms: int | None = None
    ) -> None:
        self._uri = uri
        self._duration_ms = max(duration_ms or 0, 0)
        self._base_ms = max(start_ms, 0)
        self._position_ms = self._base_ms
        self._playing = False
        self._started_at = None
        self._eof = False
        self._error = None

    async def play(self) -> None:
        if self._playing:
            return
        self._base_ms = self._position_ms
        self._started_at = time.monotonic()
        self._playing = True

    async def pause(self) -> None:
        if not self._playing:
            return
        self._position_ms = self.get_position_ms()
        self._base_ms = self._position_ms
        self._playing = False
        self._started_at = None

    async def stop(self) -> None:
        self._playing = False
        self._started_at = None
        self._position_ms = 0
        self._base_ms = 0
        self._eof = False

    async def seek(self, position_ms: int) -> None:
        clamped = max(position_ms, 0)
        if self._duration_ms:
            clamped = min(clamped, self._duration_ms)
        self._base_ms = clamped
        self._position_ms = clamped
        if self._playing:
            self._started_at = time.monotonic()
        self._eof = False

    async def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, int(volume)))

    async def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def get_position_ms(self) -> int:
        if not self._playing or self._started_at is None:
            position = self._position_ms
        else:
            elapsed_ms = (time.monotonic() - self._started_at) * 1000
            position = int(self._base_ms + elapsed_ms)
        if self._duration_ms:
            position = min(position, self._duration_ms)
        return max(position, 0)

    def get_duration_ms(self) -> int:
        return self._duration_ms

    def take_eof(self) -> bool:
        eof, self._eof = self._eof, False
        return eof

    def take_error(self) -> str | None:
        error, self._error = self._error, None
        return error

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "ready": self.ready,
            "uri": self._uri,
            "volume": self._volume,
            "muted": self._muted,
        }
