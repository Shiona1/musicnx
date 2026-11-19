"""System metrics sampler and power actions (no real reboot)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.errors import UnavailableError
from app.services.metrics import SystemMetricsService
from app.services.power import CommandResult, PowerService
from tests.conftest import make_settings


def test_metrics_sample_parses_proc_and_sysfs(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(
        "cpu  100 0 100 800 0 0 0 0 0 0\n"
        "cpu0 100 0 100 800 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    (proc / "loadavg").write_text("0.42 0.30 0.20 1/100 1234\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal:        1000000 kB\n"
        "MemFree:          200000 kB\n"
        "MemAvailable:     600000 kB\n",
        encoding="utf-8",
    )
    (proc / "uptime").write_text("12345.67 0.00\n", encoding="utf-8")
    thermal = tmp_path / "temp"
    thermal.write_text("42000\n", encoding="utf-8")
    cpufreq = tmp_path / "freq"
    cpufreq.write_text("1200000\n", encoding="utf-8")

    service = SystemMetricsService(
        make_settings(tmp_path),
        proc_root=proc,
        thermal_path=thermal,
        cpufreq_path=cpufreq,
        disk_path=tmp_path,
        vcgencmd=None,
        interval_s=3600,
    )

    async def run() -> dict:
        await service.start()
        try:
            # The second sample has a previous CPU baseline and yields a percent.
            (proc / "stat").write_text(
                "cpu  200 0 100 900 0 0 0 0 0 0\n"
                "cpu0 200 0 100 900 0 0 0 0 0 0\n",
                encoding="utf-8",
            )
            await service._collect_and_store()
            return await service.snapshot(60)
        finally:
            await service.stop()

    payload = asyncio.run(run())
    sample = payload["samples"][-1]
    assert sample["load1"] == 0.42
    assert sample["mem_total_kb"] == 1000000
    assert sample["mem_used_kb"] == 400000
    assert sample["temp_c"] == 42.0
    assert sample["clock_mhz"] == 1200
    assert sample["uptime_s"] == 12345.7
    assert sample["cpu_pct"] is not None
    assert 0 <= sample["cpu_pct"] <= 100
    assert sample["disk_free_bytes"] > 0


async def test_power_request_checks_sudo_then_schedules() -> None:
    calls: list[tuple[str, ...]] = []

    async def runner(args, _timeout):  # type: ignore[no-untyped-def]
        calls.append(tuple(args))
        return CommandResult(0)

    service = PowerService(runner=runner, delay_s=0)  # type: ignore[arg-type]
    await service.request("reboot")
    assert service._scheduled is not None
    await service._scheduled

    assert calls == [
        ("sudo", "-n", "true"),
        ("sudo", "-n", "systemctl", "reboot"),
    ]
    await service.close()


async def test_power_request_rejects_without_passwordless_sudo() -> None:
    async def runner(_args, _timeout):  # type: ignore[no-untyped-def]
        return CommandResult(1, stderr="password is required")

    service = PowerService(runner=runner, delay_s=0)  # type: ignore[arg-type]
    with pytest.raises(UnavailableError):
        await service.request("poweroff")
    assert service._scheduled is None


def test_metrics_snapshot_on_non_linux_is_empty(tmp_path: Path) -> None:
    service = SystemMetricsService(
        make_settings(tmp_path),
        proc_root=tmp_path / "missing-proc",
        thermal_path=tmp_path / "missing-temp",
        cpufreq_path=tmp_path / "missing-freq",
        disk_path=tmp_path,
        vcgencmd=None,
        interval_s=3600,
    )

    async def run() -> dict:
        await service.start()
        try:
            return await service.snapshot(60)
        finally:
            await service.stop()

    payload = asyncio.run(run())
    assert payload["samples"], "the sampler still emits a sample with null fields"
    sample = payload["samples"][-1]
    assert sample["cpu_pct"] is None
    assert sample["temp_c"] is None
    assert sample["throttled"] is None
