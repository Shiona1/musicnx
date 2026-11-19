"""Audio router boundary and a deterministic mock.

Real PipeWire routing, USB-DAC priority, Bluetooth A2DP and per-zone volume are
implemented in P7. The mock exposes one virtual local output so the rest of the
system can be wired and tested without audio hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AudioOutput:
    id: str
    kind: str
    name: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "name": self.name}


class AudioRouter(Protocol):
    async def list_outputs(self) -> list[AudioOutput]: ...

    async def describe(self) -> dict[str, Any]: ...


class MockAudioRouter:
    backend = "mock"

    async def list_outputs(self) -> list[AudioOutput]:
        return [AudioOutput(id="mock-local", kind="local_sink", name="Mock output")]

    async def describe(self) -> dict[str, Any]:
        outputs = await self.list_outputs()
        return {"backend": self.backend, "outputs": [output.as_dict() for output in outputs]}
