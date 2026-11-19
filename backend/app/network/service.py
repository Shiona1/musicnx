"""NetworkManager (``nmcli``) integration for Wi-Fi status and control.

This is the real adapter behind the P8 network API. Every external call uses
``asyncio.create_subprocess_exec`` with an argument list — never a shell — and a
bounded timeout, so a missing NetworkManager or a hung radio cannot block the
event loop.

Mock-first (AGENTS.md §4): mock mode, or real mode on a host without ``nmcli``,
uses a deterministic in-memory backend and reports ``backend="mock"`` so the API
never claims hardware that is absent.

Passwords only exist in the argv of the connect command. They are never logged,
persisted or returned; failures expose only the process return code.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.config import Settings
from app.core.event_bus import EventBus
from app.core.models import NetworkStatus, WifiNetwork, WifiScanPage
from app.errors import UnavailableError
from app.logging_setup import get_logger

logger = get_logger("network.service")

NetworkBackend = Literal["mock", "nmcli"]

#: Every nmcli process is bounded; docs/API.md §13 allows 10-20 seconds.
DEFAULT_TIMEOUT_S = 15.0
MISSING_COMMAND_CODE = 127
TIMEOUT_CODE = 124
SSID_MAX_BYTES = 32
PASSWORD_MAX_LENGTH = 128

MOCK_INTERFACE = "mock0"
MOCK_IP = "192.168.1.42"
MOCK_ACTIVE_SSID = "Mock Home"
#: Deterministic mock networks, strongest first.
MOCK_NETWORKS: tuple[WifiNetwork, ...] = (
    WifiNetwork(ssid="Mock Home", signal=82, security="WPA2"),
    WifiNetwork(ssid="Mock Guest", signal=64, security="WPA2"),
    WifiNetwork(ssid="Mock Cafe", signal=41, security=None),
)

#: (argv, timeout_s) -> result. Injected in tests; production uses
#: :func:`run_network_command`.
NetworkCommandRunner = Callable[
    [Sequence[str], float], Awaitable["NetworkCommandResult"]
]


@dataclass(frozen=True)
class NetworkCommandResult:
    """Outcome of one bounded ``nmcli`` process."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _close_pipes(process: asyncio.subprocess.Process) -> None:
    """Release pipe transports after a killed process (Windows proactor)."""

    for stream in (process.stdout, process.stderr):
        transport = getattr(stream, "_transport", None) if stream is not None else None
        if transport is not None:
            transport.close()


async def run_network_command(
    args: Sequence[str], timeout_s: float = DEFAULT_TIMEOUT_S
) -> NetworkCommandResult:
    """Run one command without a shell and kill it when the timeout expires."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # includes FileNotFoundError
        return NetworkCommandResult(
            returncode=MISSING_COMMAND_CODE, stderr=str(exc)
        )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:  # the process exited at the timeout boundary
            pass
        await process.wait()
        _close_pipes(process)
        return NetworkCommandResult(
            returncode=TIMEOUT_CODE, stderr="command timed out"
        )
    except asyncio.CancelledError:
        # Do not leave an orphan process behind when the request is cancelled.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        _close_pipes(process)
        raise
    return NetworkCommandResult(
        returncode=process.returncode if process.returncode is not None else 1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


def validate_ssid(ssid: str) -> str:
    """Validate an SSID before it reaches an argument list."""

    if not isinstance(ssid, str) or not ssid:
        raise ValueError("SSID must not be empty")
    if len(ssid.encode("utf-8")) > SSID_MAX_BYTES:
        raise ValueError(f"SSID must be at most {SSID_MAX_BYTES} UTF-8 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in ssid):
        raise ValueError("SSID must not contain control characters")
    return ssid


def validate_password(password: str) -> str:
    """Validate a Wi-Fi passphrase; an empty value means an open network."""

    if not isinstance(password, str):
        raise ValueError("password must be a string")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"password must be at most {PASSWORD_MAX_LENGTH} characters")
    if "\x00" in password:
        raise ValueError("password must not contain NUL")
    return password


def _connect_args(ssid: str, password: str) -> tuple[str, ...]:
    """Build the connect argv; open networks omit the password option."""

    args = ("device", "wifi", "connect", ssid)
    if password:
        args = (*args, "password", password)
    return args


def split_terse_fields(line: str) -> list[str]:
    """Split one ``nmcli -t`` line, honouring backslash-escaped ``:`` and ``\\``."""

    fields: list[str] = []
    buffer: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            if char in {":", "\\"}:
                buffer.append(char)
            else:
                # nmcli only escapes separators/backslashes; keep anything else.
                buffer.append("\\")
                buffer.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    if escaped:
        buffer.append("\\")
    fields.append("".join(buffer))
    return fields


@dataclass(frozen=True)
class DeviceStatus:
    """One row of ``nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status``."""

    device: str
    kind: str
    state: str
    connection: str | None = None

    @property
    def connected(self) -> bool:
        return self.state.lower().startswith("connected")


def parse_device_status(stdout: str) -> list[DeviceStatus]:
    """Parse terse device rows; malformed lines are ignored."""

    devices: list[DeviceStatus] = []
    for line in stdout.splitlines():
        if not line:
            continue
        fields = split_terse_fields(line)
        if len(fields) < 3:
            continue
        connection = fields[3] if len(fields) > 3 else ""
        device = fields[0].strip()
        kind = fields[1].strip().lower()
        if not device:
            continue
        devices.append(
            DeviceStatus(
                device=device,
                kind=kind,
                state=fields[2].strip(),
                connection=connection or None,
            )
        )
    return devices


def _to_signal(value: str) -> int:
    try:
        signal = int(value.strip())
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, signal))


def _clean_security(value: str) -> str | None:
    security = value.strip()
    if security.casefold() in {"", "--", "none"}:
        return None
    return security


def parse_wifi_networks(stdout: str) -> list[WifiNetwork]:
    """Parse ``nmcli -t -f active,ssid,signal,security device wifi list``.

    Multiple access points can advertise the same SSID. The strongest signal
    wins, while ``connected`` is sticky across duplicates. Hidden/empty SSIDs are
    skipped; the result is sorted by signal (strongest first), then SSID.
    """

    best: dict[str, WifiNetwork] = {}
    for line in stdout.splitlines():
        if not line:
            continue
        fields = split_terse_fields(line)
        if len(fields) < 4:
            continue
        active, ssid, signal_raw, security_raw = fields[:4]
        if not ssid:
            continue
        connected = active.strip().casefold() in {"yes", "1", "true"}
        signal = _to_signal(signal_raw)
        security = _clean_security(security_raw)
        previous = best.get(ssid)
        if previous is None:
            best[ssid] = WifiNetwork(
                ssid=ssid,
                signal=signal,
                security=security,
                connected=connected,
            )
            continue
        best[ssid] = WifiNetwork(
            ssid=ssid,
            signal=max(previous.signal, signal),
            security=previous.security or security,
            connected=previous.connected or connected,
        )
    return sorted(
        best.values(), key=lambda network: (-network.signal, network.ssid.casefold(), network.ssid)
    )


def parse_ip4_address(stdout: str) -> str | None:
    """Return the first IPv4 address from ``nmcli ... device show`` output."""

    for line in stdout.splitlines():
        key, separator, value = line.partition(":")
        if not separator or not key.startswith("IP4.ADDRESS"):
            continue
        address = value.split("/", 1)[0].strip()
        if address:
            return address
    return None


def _select_active_device(devices: Sequence[DeviceStatus]) -> DeviceStatus | None:
    """Prefer the active Wi-Fi device, then Ethernet, then any non-loopback."""

    active = [
        device
        for device in devices
        if device.connected and device.kind not in {"loopback", "unmanaged"}
    ]
    for kind in ("wifi", "ethernet"):
        for device in active:
            if device.kind == kind:
                return device
    return active[0] if active else None


class NetworkService:
    """Wi-Fi status, scan, connect and disconnect over NetworkManager."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        *,
        runner: NetworkCommandRunner | None = None,
        executable: str = "nmcli",
        backend: NetworkBackend | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        # Kept for symmetry with the other P8 services; the API contract has no
        # network WebSocket event yet, so the service does not publish here.
        self._bus = bus
        self._executable = executable
        self._runner: NetworkCommandRunner = runner or run_network_command
        self._timeout_s = timeout_s
        if backend is not None:
            selected: NetworkBackend = backend
        elif runner is not None:
            # An injected runner is the test/embedding seam: treat it as real,
            # even when the surrounding settings are mock.
            selected = "nmcli"
        elif settings.mode == "mock":
            selected = "mock"
        elif shutil.which(executable) is not None:
            selected = "nmcli"
        else:
            selected = "mock"
            logger.warning(
                "%s is not installed: network API runs in mock mode", executable
            )
        self.backend = selected
        self._mock_lock = asyncio.Lock()
        self._mock_connected = True
        self._mock_ssid: str | None = MOCK_ACTIVE_SSID

    # ------------------------------------------------------------------ queries

    async def status(self) -> NetworkStatus:
        """Current connectivity, Wi-Fi SSID/signal and best-effort internet flag."""

        if self.backend == "mock":
            async with self._mock_lock:
                return self._mock_status_locked()
        return await self._status_nmcli()

    async def scan(self) -> WifiScanPage:
        """Scan for nearby networks; no password or credential is returned."""

        if self.backend == "mock":
            async with self._mock_lock:
                items = [
                    network.model_copy(
                        update={
                            "connected": self._mock_connected
                            and network.ssid == self._mock_ssid
                        }
                    )
                    for network in MOCK_NETWORKS
                ]
            items.sort(
                key=lambda network: (
                    -network.signal,
                    network.ssid.casefold(),
                    network.ssid,
                )
            )
            return WifiScanPage(items=items, total=len(items), backend=self.backend)

        result = await self._run(
            ("-t", "-f", "active,ssid,signal,security", "device", "wifi", "list")
        )
        if not result.ok:
            raise UnavailableError(
                "wifi scan failed", details={"returncode": result.returncode}
            )
        items = parse_wifi_networks(result.stdout)
        return WifiScanPage(items=items, total=len(items), backend=self.backend)

    # ------------------------------------------------------------------ mutations

    async def connect(self, ssid: str, password: str) -> NetworkStatus:
        """Join a Wi-Fi network. The password never leaves the argv of nmcli."""

        network_ssid = validate_ssid(ssid)
        validate_password(password)

        if self.backend == "mock":
            async with self._mock_lock:
                self._mock_connected = True
                self._mock_ssid = network_ssid
            logger.info("wifi connected (mock): %r", network_ssid)
            return await self.status()

        result = await self._run(_connect_args(network_ssid, password))
        if not result.ok:
            # Never echo stderr: it is not needed for the API contract and could
            # contain data we do not want to surface.
            logger.warning(
                "wifi connect failed for %r (returncode=%s)",
                network_ssid,
                result.returncode,
            )
            raise UnavailableError(
                "wifi connection failed",
                details={"returncode": result.returncode},
            )
        logger.info("wifi connected: %r", network_ssid)
        return await self.status()

    async def disconnect(self) -> NetworkStatus:
        """Disconnect the active Wi-Fi interface, leaving Ethernet untouched."""

        if self.backend == "mock":
            async with self._mock_lock:
                self._mock_connected = False
            logger.info("wifi disconnected (mock)")
            return await self.status()

        status = await self._run(
            ("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
        )
        if not status.ok:
            raise UnavailableError(
                "nmcli is unavailable", details={"returncode": status.returncode}
            )
        active_wifi = next(
            (
                device
                for device in parse_device_status(status.stdout)
                if device.connected and device.kind == "wifi"
            ),
            None,
        )
        if active_wifi is None:
            return await self.status()

        result = await self._run(("device", "disconnect", active_wifi.device))
        if not result.ok:
            raise UnavailableError(
                "wifi disconnect failed",
                details={"returncode": result.returncode},
            )
        logger.info("wifi disconnected: %r", active_wifi.device)
        return await self.status()

    # ------------------------------------------------------------------ internals

    def _mock_status_locked(self) -> NetworkStatus:
        ssid = self._mock_ssid if self._mock_connected else None
        signal = None
        if ssid is not None:
            signal = next(
                (network.signal for network in MOCK_NETWORKS if network.ssid == ssid),
                0,
            )
        return NetworkStatus(
            backend=self.backend,
            connected=self._mock_connected,
            interface=MOCK_INTERFACE,
            ssid=ssid,
            ip=MOCK_IP if self._mock_connected else None,
            signal=signal,
            internet=self._mock_connected,
        )

    async def _status_nmcli(self) -> NetworkStatus:
        result = await self._run(
            ("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
        )
        if not result.ok:
            raise UnavailableError(
                "nmcli is unavailable", details={"returncode": result.returncode}
            )
        active = _select_active_device(parse_device_status(result.stdout))
        if active is None:
            return NetworkStatus(
                backend=self.backend,
                connected=False,
                interface=None,
                internet=await self._internet_available(),
                message="no active connection",
            )

        ssid: str | None = None
        signal: int | None = None
        if active.kind == "wifi":
            wifi_result = await self._run(
                ("-t", "-f", "active,ssid,signal,security", "device", "wifi", "list")
            )
            if wifi_result.ok:
                current = next(
                    (
                        network
                        for network in parse_wifi_networks(wifi_result.stdout)
                        if network.connected
                    ),
                    None,
                )
                if current is not None:
                    ssid = current.ssid
                    signal = current.signal
            if ssid is None:
                # The profile name is not guaranteed to equal the SSID, but it
                # is more useful than reporting nothing when nmcli omits the
                # active row (for example, during a roam).
                ssid = active.connection

        ip, internet = await asyncio.gather(
            self._ip_address(active.device),
            self._internet_available(),
        )
        return NetworkStatus(
            backend=self.backend,
            connected=True,
            interface=active.device,
            ssid=ssid,
            ip=ip,
            signal=signal,
            internet=internet,
        )

    async def _ip_address(self, device: str) -> str | None:
        result = await self._run(("-t", "-f", "IP4.ADDRESS", "device", "show", device))
        if not result.ok:
            return None
        return parse_ip4_address(result.stdout)

    async def _internet_available(self) -> bool:
        """Best-effort connectivity; ``connectivity check`` needs auth on some hosts."""

        try:
            result = await self._run(("-t", "-f", "CONNECTIVITY", "general"))
            if result.ok and result.stdout.strip():
                return result.stdout.strip().casefold() == "full"
            result = await self._run(("networking", "connectivity", "check"))
        except Exception:  # pragma: no cover - defensive, runner returns codes
            logger.debug("network connectivity check failed", exc_info=True)
            return False
        return result.ok and result.stdout.strip().casefold() == "full"

    async def _run(
        self, args: Sequence[str], timeout_s: float | None = None
    ) -> NetworkCommandResult:
        command = (self._executable, *args)
        try:
            return await self._runner(
                command, self._timeout_s if timeout_s is None else timeout_s
            )
        except TimeoutError:
            return NetworkCommandResult(
                returncode=TIMEOUT_CODE, stderr="command timed out"
            )
        except OSError as exc:
            return NetworkCommandResult(
                returncode=MISSING_COMMAND_CODE, stderr=str(exc)
            )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MOCK_ACTIVE_SSID",
    "MOCK_INTERFACE",
    "MOCK_IP",
    "MOCK_NETWORKS",
    "PASSWORD_MAX_LENGTH",
    "SSID_MAX_BYTES",
    "DeviceStatus",
    "NetworkBackend",
    "NetworkCommandResult",
    "NetworkCommandRunner",
    "NetworkService",
    "parse_device_status",
    "parse_ip4_address",
    "parse_wifi_networks",
    "run_network_command",
    "split_terse_fields",
    "validate_password",
    "validate_ssid",
]
