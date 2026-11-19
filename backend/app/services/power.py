"""Reboot / power-off for the web UI.

The backend runs as an ordinary user service; the Pi grants it passwordless sudo
for ``systemctl reboot|poweroff`` (sudoers drop-in). The action is scheduled a
couple of seconds after the HTTP response so the client gets a clear 202 instead
of a dropped connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.errors import UnavailableError
from app.logging_setup import get_logger

logger = get_logger("services.power")

PowerAction = Literal["reboot", "poweroff"]

CommandRunner = Callable[[Sequence[str], float], Awaitable["CommandResult"]]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_command(args: Sequence[str], timeout_s: float) -> CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return CommandResult(127, stderr=str(exc))
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return CommandResult(124, stderr="command timed out")
    return CommandResult(
        returncode=process.returncode if process.returncode is not None else 1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


class PowerService:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        delay_s: float = 2.0,
        executable: str = "sudo",
    ) -> None:
        self._runner = runner or run_command
        self._delay_s = delay_s
        self._executable = executable
        self._scheduled: asyncio.Task[None] | None = None

    async def request(self, action: PowerAction) -> None:
        """Validate permissions, then schedule the action."""

        if action not in ("reboot", "poweroff"):
            raise ValueError("unsupported power action")
        if self._scheduled is not None and not self._scheduled.done():
            raise UnavailableError(
                "a power action is already scheduled",
                details={"action": action},
            )
        check = await self._runner((self._executable, "-n", "true"), 4.0)
        if not check.ok:
            raise UnavailableError(
                "power action needs passwordless sudo for systemctl",
                details={"returncode": check.returncode},
            )
        self._scheduled = asyncio.create_task(
            self._run_later(action), name=f"power-{action}"
        )

    async def _run_later(self, action: PowerAction) -> None:
        await asyncio.sleep(self._delay_s)
        command = (self._executable, "-n", "systemctl", action)
        logger.warning("executing power action: %s", action)
        try:
            result = await self._runner(command, 15.0)
            if not result.ok:
                logger.error(
                    "power action %s failed: rc=%s stderr=%s",
                    action,
                    result.returncode,
                    result.stderr[:200],
                )
        except Exception:  # pragma: no cover - machine is about to go down
            logger.exception("power action %s crashed", action)

    async def execute(self, action: PowerAction) -> CommandResult:
        """Run the command immediately (used by tests and manual recovery)."""

        return await self._runner((self._executable, "-n", "systemctl", action), 15.0)

    async def close(self) -> None:
        if self._scheduled is not None:
            self._scheduled.cancel()
            try:
                await self._scheduled
            except asyncio.CancelledError:
                pass
            self._scheduled = None


__all__ = ["CommandResult", "PowerAction", "PowerService", "run_command"]
