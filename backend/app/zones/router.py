"""PipeWire routing boundary: sink discovery, default-sink switching, volume.

Real mode drives ``pactl`` through ``asyncio.create_subprocess_exec`` — arguments
are always a list, never a shell (AGENTS.md §6, SPEC §7.4). Every call is bounded
by a timeout, so a hung PulseAudio/PipeWire daemon cannot stall the event loop.

When ``pactl`` is not installed the real router silently degrades to
:class:`MockZoneRouter`: the backend is then reported as ``"mock"``, so callers
never mistake a virtual sink for real hardware (AGENTS.md §12).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings
from app.logging_setup import get_logger

logger = get_logger("zones.router")

DEFAULT_TIMEOUT_S = 2.0
#: ``pactl`` exit code used by our runner when the binary cannot be executed.
MISSING_COMMAND_CODE = 127
TIMEOUT_CODE = 124

CommandRunner = Callable[[Sequence[str], float], Awaitable["CommandResult"]]


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one bounded subprocess call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class SinkInfo:
    """One available PipeWire/PulseAudio sink."""

    name: str
    description: str = ""
    available: bool = True


class ZoneRouter(Protocol):
    """Output routing boundary used by :class:`~app.zones.manager.ZoneManager`."""

    backend: str

    async def list_sinks(self) -> list[SinkInfo]: ...

    async def activate(self, sink_name: str) -> bool: ...

    async def set_volume(self, sink_name: str, volume: int) -> bool: ...

    async def is_available(self, sink_name: str) -> bool: ...

    async def close(self) -> None: ...


async def run_command(args: Sequence[str], timeout_s: float) -> CommandResult:
    """Run a command without a shell, decode its output, enforce a timeout."""

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # includes FileNotFoundError
        return CommandResult(returncode=MISSING_COMMAND_CODE, stderr=str(exc))
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        # Do not leave a zombie behind when the daemon stops answering.
        process.kill()
        await process.wait()
        return CommandResult(returncode=TIMEOUT_CODE, stderr="command timed out")
    return CommandResult(
        returncode=process.returncode if process.returncode is not None else 1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


def _parse_sinks(stdout: str) -> list[SinkInfo]:
    """Parse ``pactl list short sinks`` (tab-separated) into sink DTOs."""

    sinks: list[SinkInfo] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2 or not parts[1]:
            continue
        state = parts[4].strip().upper() if len(parts) > 4 else ""
        sinks.append(
            SinkInfo(
                name=parts[1].strip(),
                description=parts[1].strip(),
                # SUSPENDED/IDLE/RUNNING are all usable; only UNAVAILABLE is not.
                available=state != "UNAVAILABLE",
            )
        )
    return sinks


def _parse_sink_input_ids(stdout: str) -> list[str]:
    """First column of ``pactl list short sink-inputs`` is the input index."""

    ids: list[str] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
        if parts and parts[0].strip().isdigit():
            ids.append(parts[0].strip())
    return ids


class PipeWireRouter:
    """Real PipeWire router backed by ``pactl`` (pipewire-pulse)."""

    def __init__(
        self,
        *,
        executable: str = "pactl",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        runner: CommandRunner | None = None,
    ) -> None:
        self._executable = executable
        self._timeout_s = timeout_s
        self._runner: CommandRunner = runner or run_command
        # A caller-injected runner is already a test/embedding boundary: trust it.
        self._fallback = (
            MockZoneRouter()
            if runner is None and shutil.which(executable) is None
            else None
        )
        if self._fallback is not None:
            logger.warning("pactl is not installed: audio zones fall back to mock")

    @property
    def backend(self) -> str:
        """``pactl`` when real routing is live, ``mock`` when degraded."""

        return "mock" if self._fallback is not None else "pactl"

    async def _run(self, *args: str) -> CommandResult:
        return await self._runner((self._executable, *args), self._timeout_s)

    async def list_sinks(self) -> list[SinkInfo]:
        if self._fallback is not None:
            return await self._fallback.list_sinks()
        result = await self._run("list", "short", "sinks")
        if not result.ok:
            logger.warning("pactl list short sinks failed: %s", result.returncode)
            return []
        return _parse_sinks(result.stdout)

    async def activate(self, sink_name: str) -> bool:
        """Make ``sink_name`` the default and move current streams onto it.

        mpv is the only audio producer, so moving every sink-input is equivalent
        to moving mpv's and keeps the track playing without a restart.
        """

        if self._fallback is not None:
            return await self._fallback.activate(sink_name)
        result = await self._run("set-default-sink", sink_name)
        if not result.ok:
            logger.warning("pactl set-default-sink failed: %s", result.returncode)
            return False
        inputs = await self._run("list", "short", "sink-inputs")
        if inputs.ok:
            for input_id in _parse_sink_input_ids(inputs.stdout):
                moved = await self._run("move-sink-input", input_id, sink_name)
                if not moved.ok:
                    # The default sink already changed; a failed move is logged,
                    # not fatal (the stream may have ended in the meantime).
                    logger.warning(
                        "pactl move-sink-input %s failed: %s",
                        input_id,
                        moved.returncode,
                    )
        return True

    async def set_volume(self, sink_name: str, volume: int) -> bool:
        if not 0 <= volume <= 100:
            raise ValueError("volume must be between 0 and 100")
        if self._fallback is not None:
            return await self._fallback.set_volume(sink_name, volume)
        result = await self._run("set-sink-volume", sink_name, f"{volume}%")
        if not result.ok:
            logger.warning("pactl set-sink-volume failed: %s", result.returncode)
        return result.ok

    async def is_available(self, sink_name: str) -> bool:
        return any(sink.name == sink_name for sink in await self.list_sinks())

    async def close(self) -> None:
        """No persistent resources: each ``pactl`` call is one short process."""

    async def __aenter__(self) -> PipeWireRouter:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()


class MockZoneRouter:
    """Deterministic virtual router used in mock mode and by unit tests."""

    backend = "mock"

    def __init__(self, sinks: Sequence[SinkInfo] | None = None) -> None:
        self._sinks = list(
            sinks or [SinkInfo(name="mock_local", description="Mock local output")]
        )
        self._default_sink = self._sinks[0].name if self._sinks else None
        self._volumes: dict[str, int] = {}

    @property
    def default_sink(self) -> str | None:
        return self._default_sink

    async def list_sinks(self) -> list[SinkInfo]:
        return list(self._sinks)

    async def activate(self, sink_name: str) -> bool:
        if not any(sink.name == sink_name for sink in self._sinks):
            # Bluetooth A2DP sinks appear dynamically once a device connects; the
            # mock mirrors that instead of failing a legitimate registration.
            self._sinks.append(
                SinkInfo(name=sink_name, description=sink_name, available=True)
            )
        self._default_sink = sink_name
        return True

    async def set_volume(self, sink_name: str, volume: int) -> bool:
        if not 0 <= volume <= 100:
            raise ValueError("volume must be between 0 and 100")
        if not any(sink.name == sink_name for sink in self._sinks):
            self._sinks.append(
                SinkInfo(name=sink_name, description=sink_name, available=True)
            )
        self._volumes[sink_name] = volume
        return True

    async def is_available(self, sink_name: str) -> bool:
        return any(sink.name == sink_name for sink in self._sinks)

    async def close(self) -> None:
        """Nothing to release."""


def create_zone_router(
    settings: Settings, *, runner: CommandRunner | None = None
) -> ZoneRouter:
    """Pick the router implementation from configuration (mock-first)."""

    if settings.mode == "mock":
        return MockZoneRouter()
    return PipeWireRouter(runner=runner)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CommandResult",
    "CommandRunner",
    "MockZoneRouter",
    "PipeWireRouter",
    "SinkInfo",
    "ZoneRouter",
    "create_zone_router",
    "run_command",
]
