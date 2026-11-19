"""Bluetooth devices and A2DP pairing through BlueZ ``bluetoothctl``.

All real calls run through ``asyncio.create_subprocess_exec`` with an argument
list (no shell) and a timeout, so a missing adapter or a slow BlueZ daemon never
blocks the event loop.

Mock-first (AGENTS.md §4): mock mode uses an in-memory, deterministic device set.
Real mode falls back to the same mock when ``bluetoothctl`` is not installed, and
reports ``backend="mock"`` so the API never claims real hardware that is absent
(AGENTS.md §12). Pairing codes are handled by BlueZ/agent on the Pi; no PIN or
secret is read, logged or returned here.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import BluetoothDevice, BluetoothDevicePage
from app.errors import NotFoundError, UnavailableError
from app.logging_setup import get_logger

logger = get_logger("bluetooth")

BluetoothBackend = Literal["mock", "bluetoothctl"]

#: Canonical six-octet MAC address. Validation happens before any subprocess.
ADDRESS_PATTERN = r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$"
_ADDRESS_RE = re.compile(ADDRESS_PATTERN)
DEFAULT_TIMEOUT_S = 3.0
PAIR_TIMEOUT_S = 25.0
SCAN_TIMEOUT_S = 5.0
CONNECT_VERIFY_TIMEOUT_S = 8.0
MISSING_COMMAND_CODE = 127
TIMEOUT_CODE = 124
REASON_MAX_LEN = 180

BluetoothCommandRunner = Callable[
    [Sequence[str], float], Awaitable["BluetoothCommandResult"]
]

#: Deterministic mock devices: one connected speaker, one paired headset and one
#: discoverable headset that exercises the pair flow.
MOCK_DEVICES: tuple[BluetoothDevice, ...] = (
    BluetoothDevice(
        address="AA:BB:CC:DD:EE:01",
        name="Mock Speaker",
        paired=True,
        connected=True,
    ),
    BluetoothDevice(
        address="AA:BB:CC:DD:EE:02",
        name="Mock Headphones",
        paired=True,
        connected=False,
    ),
    BluetoothDevice(
        address="AA:BB:CC:DD:EE:03",
        name="Mock Earbuds",
        paired=False,
        connected=False,
    ),
)


@dataclass(frozen=True)
class BluetoothCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_bluetooth_command(
    args: Sequence[str], timeout_s: float
) -> BluetoothCommandResult:
    """Run one bounded subprocess without a shell."""

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # includes FileNotFoundError
        return BluetoothCommandResult(
            returncode=MISSING_COMMAND_CODE, stderr=str(exc)
        )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return BluetoothCommandResult(
            returncode=TIMEOUT_CODE, stderr="command timed out"
        )
    return BluetoothCommandResult(
        returncode=process.returncode if process.returncode is not None else 1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


def _normalize_address(address: str) -> str:
    return address.strip().upper()


def _parse_devices(stdout: str) -> list[tuple[str, str | None]]:
    """Parse ``bluetoothctl devices`` lines: ``Device AA:BB:.. Name``."""

    devices: list[tuple[str, str | None]] = []
    for line in stdout.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) < 2 or parts[0] != "Device":
            continue
        address = _normalize_address(parts[1])
        name = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        devices.append((address, name))
    return devices


def _command_reason(result: BluetoothCommandResult) -> str:
    """First meaningful BlueZ line, safe to show to the owner (no secrets)."""

    for raw in (result.stderr, result.stdout):
        for line in raw.splitlines():
            text = line.strip()
            if text:
                return text[:REASON_MAX_LEN]
    return "bluetoothctl reported an error"


class BluetoothService:
    """Device discovery, pairing, connection and forgetting."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        *,
        runner: BluetoothCommandRunner | None = None,
        executable: str = "bluetoothctl",
        backend: BluetoothBackend | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._executable = executable
        self._runner: BluetoothCommandRunner = runner or run_bluetooth_command
        self._timeout_s = timeout_s
        if backend is not None:
            selected: BluetoothBackend = backend
        elif settings.mode == "mock":
            selected = "mock"
        elif runner is not None:
            # An injected runner is a test/embedding seam: treat it as real.
            selected = "bluetoothctl"
        elif shutil.which(executable) is not None:
            selected = "bluetoothctl"
        else:
            selected = "mock"
            logger.warning(
                "%s is not installed: bluetooth API runs in mock mode", executable
            )
        self.backend = selected
        self._mock: dict[str, BluetoothDevice] = {
            device.address: device.model_copy() for device in MOCK_DEVICES
        }
        # Last known device count, so /system/status does not spawn extra
        # bluetoothctl processes on every poll.
        self._known_device_count = len(self._mock) if self.backend == "mock" else 0
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------- queries

    async def devices(self) -> BluetoothDevicePage:
        if self.backend == "mock":
            async with self._lock:
                items = sorted(self._mock.values(), key=lambda item: item.address)
            self._known_device_count = len(items)
            return BluetoothDevicePage(
                items=items, total=len(items), backend=self.backend
            )
        items = await self._read_devices()
        self._known_device_count = len(items)
        return BluetoothDevicePage(items=items, total=len(items), backend=self.backend)

    async def scan(self, *, timeout_s: float = SCAN_TIMEOUT_S) -> BluetoothDevicePage:
        """Run a bounded discovery scan and return the known device list."""

        if self.backend == "mock":
            return await self.devices()
        result = await self._run(
            ("--timeout", f"{timeout_s:g}", "scan", "on"), timeout_s + 5.0
        )
        if not result.ok and result.returncode != TIMEOUT_CODE:
            raise UnavailableError(
                "bluetooth scan failed", details={"returncode": result.returncode}
            )
        return await self.devices()

    async def status(self) -> dict[str, Any]:
        """Compact cached adapter status for ``/system/status`` (no secrets)."""

        adapter = "mock" if self.backend == "mock" else "on"
        return {
            "backend": self.backend,
            "adapter": adapter,
            "devices": self._known_device_count,
        }

    # ------------------------------------------------------------------ mutations

    async def pair(self, address: str) -> BluetoothDevice:
        normalized = self._validate_address(address)
        device = await self._find(normalized)
        if device.paired:
            return device
        if self.backend == "mock":
            updated = device.model_copy(update={"paired": True})
            async with self._lock:
                self._mock[normalized] = updated
            await self._publish("bt.device_paired", updated)
            return updated
        result = await self._run(("pair", normalized), PAIR_TIMEOUT_S)
        if not result.ok:
            reason = _command_reason(result)
            raise UnavailableError(
                f"bluetooth pairing failed: {reason}",
                details={"returncode": result.returncode, "reason": reason},
            )
        # Trusting a paired device lets auto-reconnect work without prompting.
        await self._run(("trust", normalized), self._timeout_s)
        refreshed = await self._find_or_default(normalized, paired=True)
        await self._publish("bt.device_paired", refreshed)
        return refreshed

    async def connect(self, address: str) -> BluetoothDevice:
        normalized = self._validate_address(address)
        device = await self._find(normalized)
        if device.connected:
            return device
        if self.backend == "mock":
            updated = device.model_copy(update={"paired": True, "connected": True})
            async with self._lock:
                self._mock[normalized] = updated
            await self._publish("bt.device_connected", updated)
            return updated
        result = await self._run(("connect", normalized), PAIR_TIMEOUT_S)
        if not result.ok:
            reason = _command_reason(result)
            raise UnavailableError(
                f"bluetooth connection failed: {reason}",
                details={
                    "returncode": result.returncode,
                    "reason": reason,
                    "hint": (
                        "make sure the speaker is on, in range, and not "
                        "connected to a phone; try scan again"
                    ),
                },
            )
        if not await self._wait_connected(normalized):
            raise UnavailableError(
                "bluetooth connection timed out: the A2DP link did not come up",
                details={
                    "address": normalized,
                    "hint": "reconnect from the speaker side or power-cycle it",
                },
            )
        refreshed = await self._find_or_default(
            normalized, paired=True, connected=True
        )
        await self._publish("bt.device_connected", refreshed)
        return refreshed

    async def forget(self, address: str) -> BluetoothDevice:
        normalized = self._validate_address(address)
        device = await self._find(normalized)
        if self.backend == "mock":
            forgotten = device.model_copy(
                update={"paired": False, "connected": False}
            )
            async with self._lock:
                self._mock[normalized] = forgotten
            await self._publish("bt.device_forgotten", forgotten)
            return forgotten
        result = await self._run(("remove", normalized), PAIR_TIMEOUT_S)
        if not result.ok:
            raise UnavailableError(
                "bluetooth remove failed",
                details={"returncode": result.returncode},
            )
        forgotten = BluetoothDevice(
            address=normalized, name=device.name, paired=False, connected=False
        )
        await self._publish("bt.device_forgotten", forgotten)
        return forgotten

    async def auto_reconnect(self) -> list[BluetoothDevice]:
        """Best-effort reconnect of paired devices that are not connected yet.

        Not scheduled by default: the integration may call it at startup once
        ``bluetoothctl`` is available. Mock mode returns currently connected
        devices without pretending to reconnect anything.
        """

        if self.backend == "mock":
            return [
                device
                for device in (await self.devices()).items
                if device.connected
            ]
        page = await self.devices()
        reconnected: list[BluetoothDevice] = []
        for device in page.items:
            if not device.paired or device.connected:
                continue
            try:
                reconnected.append(await self.connect(device.address))
            except (NotFoundError, UnavailableError):
                logger.warning("bluetooth auto-reconnect failed for %s", device.address)
        return reconnected

    async def close(self) -> None:
        """No persistent process: each ``bluetoothctl`` call is short-lived."""

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _validate_address(address: str) -> str:
        normalized = _normalize_address(address)
        if not _ADDRESS_RE.fullmatch(normalized):
            raise ValueError("invalid bluetooth address")
        return normalized

    async def _find(self, address: str) -> BluetoothDevice:
        page = await self.devices()
        for device in page.items:
            if device.address == address:
                return device
        raise NotFoundError(
            f"unknown bluetooth device: {address}", details={"address": address}
        )

    async def _find_or_default(
        self,
        address: str,
        *,
        paired: bool = False,
        connected: bool = False,
    ) -> BluetoothDevice:
        try:
            device = await self._find(address)
        except NotFoundError:
            return BluetoothDevice(
                address=address, paired=paired, connected=connected
            )
        # ``devices Paired|Connected`` can be stale right after a command; never
        # report a just-completed pair/connect as failed.
        return device.model_copy(
            update={
                "paired": device.paired or paired,
                "connected": device.connected or connected,
            }
        )

    async def _wait_connected(
        self, address: str, timeout_s: float = CONNECT_VERIFY_TIMEOUT_S
    ) -> bool:
        """Wait for BlueZ to report ``Connected: yes`` for a just-connected device.

        ``bluetoothctl connect`` can exit 0 before the A2DP profile finishes
        negotiating. When ``info`` is unavailable we trust the command result
        instead of inventing a failure.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            result = await self._run(("info", address), self._timeout_s)
            text = result.stdout.lower()
            if not result.ok or "connected:" not in text:
                return True
            if "connected: yes" in text:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(1.0)

    async def _read_devices(self) -> list[BluetoothDevice]:
        result = await self._run(("devices",))
        if not result.ok:
            raise UnavailableError(
                "bluetoothctl is unavailable", details={"returncode": result.returncode}
            )
        paired = await self._filtered_addresses("Paired")
        connected = await self._filtered_addresses("Connected")
        return [
            BluetoothDevice(
                address=address,
                name=name,
                paired=address in paired,
                connected=address in connected,
            )
            for address, name in _parse_devices(result.stdout)
        ]

    async def _filtered_addresses(self, kind: str) -> set[str]:
        # ``bluetoothctl devices Paired|Connected`` avoids one ``info`` call per
        # device. Older BlueZ builds may reject the filter; empty is harmless.
        result = await self._run(("devices", kind))
        if not result.ok:
            return set()
        return {address for address, _name in _parse_devices(result.stdout)}

    async def _run(
        self, args: Sequence[str], timeout_s: float | None = None
    ) -> BluetoothCommandResult:
        command = (self._executable, *args)
        return await self._runner(
            command, self._timeout_s if timeout_s is None else timeout_s
        )

    async def _publish(self, event_type: str, device: BluetoothDevice) -> None:
        await self._bus.publish(
            event_type, {"device": device.model_dump(mode="json")}
        )


def create_bluetooth_service(
    settings: Settings,
    bus: EventBus,
    *,
    runner: BluetoothCommandRunner | None = None,
) -> BluetoothService:
    """Build the service; mock mode or a missing binary yields the mock backend."""

    return BluetoothService(settings, bus, runner=runner)


__all__ = [
    "ADDRESS_PATTERN",
    "MOCK_DEVICES",
    "BluetoothBackend",
    "BluetoothCommandResult",
    "BluetoothCommandRunner",
    "BluetoothService",
    "create_bluetooth_service",
    "run_bluetooth_command",
]
