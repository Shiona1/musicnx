"""Opt-in periodic maintenance timer — P8.

There is no ``maintenance`` section in :class:`app.config.Settings`, so this
module deliberately does not invent one. The timer is **disabled by default**
and starts only when the ``settings`` table contains ``maintenance.interval_s``
(seconds, integer or float, > 0). The value is read through
:class:`~app.database.repositories.SettingsRepository`, so an operator (or a
future settings-API extension) can opt in without changing code; there is no
dedicated config key yet and none is fabricated here.

The maintenance action itself belongs to the P8 maintenance service
(``ctx.maintenance``, implemented by p8-a as
:class:`~app.maintenance.service.MaintenanceService`). The timer calls its
``maintenance()`` action when present (a plain ``run()`` coroutine or an async
callable are accepted too) and is a clean no-op otherwise — it never pretends
maintenance ran (AGENTS.md §12).

Lifecycle is owned by the integrator, because ``app/context.py`` is shared:
call ``await ctx.maintenance_timer.start()`` after context startup and
``await ctx.maintenance_timer.stop()`` during shutdown. The loop sleeps before
the first run, never blocks the event loop and survives a failing maintenance
run (the failure is logged, the timer keeps its schedule).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.database.repositories import SettingsRepository
from app.logging_setup import get_logger

logger = get_logger("maintenance.timer")

INTERVAL_KEY = "maintenance.interval_s"
#: Smallest accepted interval; guards against a busy loop from a bad value.
MIN_INTERVAL_S = 60.0


class MaintenanceTimer:
    """Background task invoking the maintenance service once per interval."""

    def __init__(
        self,
        repository: SettingsRepository,
        maintenance: Any | None = None,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self._repository = repository
        self._maintenance = maintenance
        self._min_interval_s = max(float(min_interval_s), 0.01)
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def configured_interval_s(self) -> float | None:
        """Configured period, or ``None`` when the timer is disabled."""

        value = await self._repository.get(INTERVAL_KEY)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        interval = float(value)
        if interval <= 0:
            return None
        return max(interval, self._min_interval_s)

    async def start(self) -> bool:
        """Start the loop; returns ``False`` when the timer stays disabled."""

        if self.is_running:
            return True
        interval = await self.configured_interval_s()
        if interval is None:
            logger.info("maintenance timer disabled: %s is not configured", INTERVAL_KEY)
            return False
        self._task = asyncio.create_task(self._loop(interval), name="maintenance-timer")
        logger.info("maintenance timer started: interval_s=%.1f", interval)
        return True

    async def stop(self) -> None:
        """Cancel the loop; safe to call when it never started."""

        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("maintenance timer stopped")

    async def run_once(self) -> bool:
        """Run the maintenance service now (also used by the loop).

        Returns ``True`` when a maintenance callable was executed, ``False`` for
        the documented no-op without a service.
        """

        run = self._resolve_run()
        if run is None:
            logger.debug("maintenance tick skipped: no maintenance service wired")
            return False
        await run()
        return True

    # ---------------------------------------------------------------- internals

    async def _loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a failed run must not kill the schedule
                logger.exception("maintenance run failed")

    def _resolve_run(self) -> Any | None:
        """Find the maintenance action on the wired service.

        The P8 service exposes ``maintenance()``; ``run()`` and a plain async
        callable are accepted as well, and a wrapper object with a ``.service``
        attribute is unwrapped once. Nothing is invented when none is present.
        """

        target = self._maintenance
        if target is None:
            return None
        action = _action(target)
        if action is not None:
            return action
        service = getattr(target, "service", None)
        if service is not None and service is not target:
            action = _action(service)
            if action is not None:
                return action
        return target if callable(target) else None


def _action(target: object) -> Any | None:
    for name in ("run", "maintenance"):
        action = getattr(target, name, None)
        if callable(action):
            return action
    return None


__all__ = ["INTERVAL_KEY", "MIN_INTERVAL_S", "MaintenanceTimer"]
