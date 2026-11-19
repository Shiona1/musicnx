"""In-memory system telemetry for the MusicNX status page.

A single background task samples the Pi every few seconds; the API only reads a
copy of the ring buffer, so no request ever runs ``vcgencmd`` or parses
``/proc`` on the event loop. Samples are deliberately small and ephemeral: no
database migration, no disk writes.
"""

from __future__ import annotations

import asyncio
import shutil
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logging_setup import get_logger

logger = get_logger("services.metrics")

DEFAULT_INTERVAL_S = 5.0
DEFAULT_WINDOW_MINUTES = 60
MAX_SAMPLES = 720
VERSION = 1


@dataclass(frozen=True)
class Sample:
    ts: str
    uptime_s: float | None
    cpu_pct: float | None
    load1: float | None
    mem_total_kb: int | None
    mem_used_kb: int | None
    mem_pct: float | None
    temp_c: float | None
    clock_mhz: int | None
    throttled: str | None
    disk_free_bytes: int | None


class SystemMetricsService:
    def __init__(
        self,
        settings: Settings,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        proc_root: Path = Path("/proc"),
        thermal_path: Path = Path("/sys/class/thermal/thermal_zone0/temp"),
        cpufreq_path: Path = Path(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
        ),
        disk_path: Path | None = None,
        vcgencmd: str | None = "vcgencmd",
    ) -> None:
        self._settings = settings
        self._interval_s = interval_s
        self._proc_root = proc_root
        self._thermal_path = thermal_path
        self._cpufreq_path = cpufreq_path
        self._disk_path = disk_path or settings.paths.data_dir
        self._vcgencmd = vcgencmd
        self._samples: deque[Sample] = deque(maxlen=MAX_SAMPLES)
        self._task: asyncio.Task[None] | None = None
        self._last_cpu: tuple[int, int] | None = None
        self.interval_s = interval_s

    async def start(self) -> None:
        if self._task is not None:
            return
        # First sample immediately so the page is useful on first open.
        await self._collect_and_store()
        self._task = asyncio.create_task(self._loop(), name="system-metrics")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def snapshot(self, minutes: int = DEFAULT_WINDOW_MINUTES) -> dict[str, Any]:
        minutes = max(1, min(int(minutes), 24 * 60))
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        samples = [
            asdict(sample)
            for sample in list(self._samples)
            if _parse_ts(sample.ts) >= cutoff
        ]
        return {
            "version": VERSION,
            "interval_s": self._interval_s,
            "minutes": minutes,
            "samples": samples,
        }

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self._collect_and_store()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - telemetry must not kill startup
                logger.exception("metrics sample failed")

    async def _collect_and_store(self) -> None:
        throttled = await self._read_throttled()
        sample = await asyncio.to_thread(self._sample_sync, throttled)
        self._samples.append(sample)

    def _sample_sync(self, throttled: str | None) -> Sample:
        cpu_pct = self._read_cpu_pct()
        load1 = self._read_load1()
        mem_total, mem_used = self._read_memory()
        mem_pct = (
            round(mem_used / mem_total * 100, 1)
            if mem_total and mem_used is not None
            else None
        )
        return Sample(
            ts=datetime.now(UTC).isoformat(),
            uptime_s=self._read_uptime(),
            cpu_pct=cpu_pct,
            load1=load1,
            mem_total_kb=mem_total,
            mem_used_kb=mem_used,
            mem_pct=mem_pct,
            temp_c=self._read_temp(),
            clock_mhz=self._read_clock(),
            throttled=throttled,
            disk_free_bytes=self._read_disk_free(),
        )

    # ------------------------------------------------------------------ readers

    def _read_cpu_pct(self) -> float | None:
        try:
            line = (self._proc_root / "stat").read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return None
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            values = [int(value) for value in parts[1:9]]
        except ValueError:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        if self._last_cpu is None:
            self._last_cpu = (idle, total)
            return None
        last_idle, last_total = self._last_cpu
        self._last_cpu = (idle, total)
        total_delta = total - last_total
        idle_delta = idle - last_idle
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)

    def _read_load1(self) -> float | None:
        try:
            raw = (self._proc_root / "loadavg").read_text(encoding="utf-8").split()[0]
            return round(float(raw), 2)
        except (OSError, IndexError, ValueError):
            return None

    def _read_memory(self) -> tuple[int | None, int | None]:
        try:
            lines = (self._proc_root / "meminfo").read_text(encoding="utf-8").splitlines()
        except OSError:
            return None, None
        info: dict[str, int] = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    info[parts[0].rstrip(":")] = int(parts[1])
                except ValueError:
                    continue
        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        if total is None or available is None:
            return total, None
        return total, max(total - available, 0)

    def _read_uptime(self) -> float | None:
        try:
            raw = (self._proc_root / "uptime").read_text(encoding="utf-8").split()[0]
            return round(float(raw), 1)
        except (OSError, IndexError, ValueError):
            return None

    def _read_temp(self) -> float | None:
        try:
            raw = int(self._thermal_path.read_text(encoding="utf-8").strip())
            return round(raw / 1000, 1)
        except (OSError, ValueError):
            return None

    def _read_clock(self) -> int | None:
        try:
            raw = int(self._cpufreq_path.read_text(encoding="utf-8").strip())
            return round(raw / 1000)
        except (OSError, ValueError):
            return None

    def _read_disk_free(self) -> int | None:
        try:
            return shutil.disk_usage(self._disk_path).free
        except OSError:
            return None

    async def _read_throttled(self) -> str | None:
        if self._vcgencmd is None:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                self._vcgencmd,
                "get_throttled",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None
        text = stdout.decode("utf-8", "replace").strip()
        return text.removeprefix("throttled=") if text.startswith("throttled=") else None


def _parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["MAX_SAMPLES", "Sample", "SystemMetricsService"]
