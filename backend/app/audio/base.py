"""Audio backend boundary (mock and mpv implementations live here)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AudioBackend(Protocol):
    """What the player needs from any audio engine.

    Implementations never own the queue or playback state; they only execute.
    """

    name: str

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def load(
        self, uri: str, *, start_ms: int = 0, duration_ms: int | None = None
    ) -> None: ...

    async def play(self) -> None: ...

    async def pause(self) -> None: ...

    async def stop(self) -> None: ...

    async def seek(self, position_ms: int) -> None: ...

    async def set_volume(self, volume: int) -> None: ...

    async def set_muted(self, muted: bool) -> None:
        """Mute/unmute local output without affecting the master clock."""
        ...

    def get_position_ms(self) -> int: ...

    def get_duration_ms(self) -> int: ...

    def take_eof(self) -> bool:
        """Return and clear the EOF flag set by the engine, if any."""
        ...

    def take_error(self) -> str | None:
        """Return and clear a pending engine error, if any."""
        ...

    def describe(self) -> dict[str, Any]: ...

    @property
    def ready(self) -> bool: ...
