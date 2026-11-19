"""mpv audio backend: process supervision + IPC client."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from app.audio.base import AudioBackend
from app.audio.mpv_ipc import MpvIpcClient, MpvIpcError, UnixSocketTransport
from app.config import AudioSettings
from app.logging_setup import get_logger

logger = get_logger("audio.mpv_backend")

WarningCallback = Callable[[str, str], Awaitable[None]]


class MpvAudioBackend(AudioBackend):
    """Executes commands in mpv; never owns queue or player state."""

    name = "mpv"

    def __init__(
        self,
        settings: AudioSettings,
        *,
        on_warning: WarningCallback | None = None,
    ) -> None:
        self._settings = settings
        self._on_warning = on_warning
        self._client: MpvIpcClient | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._restarting = False
        self._attempts = 0
        self._error: str | None = None
        self._warning_sent = False
        self._uri: str | None = None
        self._duration_hint_ms: int | None = None
        self._desired_playing = False
        self._last_position_ms = 0
        self._volume = 70
        self._muted = False
        self._restart_count = 0

    @property
    def ready(self) -> bool:
        return (
            self._client is not None
            and self._client.connected
            and self._process is not None
            and self._process.returncode is None
        )

    async def start(self) -> None:
        self._shutting_down = False
        await self._launch()
        self._supervisor = asyncio.create_task(self._supervise(), name="mpv-supervisor")

    async def _launch(self) -> None:
        socket_path = self._settings.ipc_socket
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - filesystem dependent
            raise RuntimeError(f"cannot remove stale mpv socket {socket_path}: {exc}") from exc

        args = [
            self._settings.mpv_path,
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            f"--input-ipc-server={socket_path}",
            f"--volume={self._volume}",
        ]
        logger.info("starting mpv: %s", " ".join(args))
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        transport = UnixSocketTransport(socket_path, self._settings.start_timeout_s)
        client = MpvIpcClient(transport)
        try:
            await client.connect()
        except Exception:
            await self._terminate_process()
            raise
        self._client = client
        await self._restore_after_launch()

    async def _restore_after_launch(self) -> None:
        if not self._uri or self._client is None:
            return
        await self._load_uri(self._client, self._uri, self._last_position_ms)
        if self._desired_playing:
            await self._client.command(["set_property", "pause", False])

    async def _load_uri(self, client: MpvIpcClient, uri: str, start_ms: int) -> None:
        """Load a file and wait until it is actually seekable."""

        client.reset_file_loaded()
        await client.command(["loadfile", uri, "replace"])
        await client.wait_for_file_loaded(timeout_s=self._settings.start_timeout_s)
        await client.command(["set_property", "pause", True])
        if start_ms > 0:
            await client.command(["seek", start_ms / 1000, "absolute"])

    async def _supervise(self) -> None:
        while not self._shutting_down:
            await asyncio.sleep(1.0)
            if self.ready or self._restarting:
                continue
            self._restarting = True
            try:
                delays = self._settings.restart_backoff_s
                delay = delays[min(self._attempts, len(delays) - 1)]
                logger.warning("mpv is not running; restart in %.1fs", delay)
                await asyncio.sleep(delay)
                if self._shutting_down:
                    return
                await self._cleanup_mpv()
                await self._launch()
                self._attempts = 0
                self._restart_count += 1
                self._warning_sent = False
                if self._on_warning is not None:
                    await self._on_warning(
                        "mpv_restarted", "mpv was restarted; playback resumed if it was active"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._attempts += 1
                self._error = str(exc)
                logger.error(
                    "mpv restart failed (%s/%s): %s",
                    self._attempts,
                    self._settings.max_restart_attempts,
                    exc,
                )
                if (
                    self._attempts >= self._settings.max_restart_attempts
                    and not self._warning_sent
                    and self._on_warning is not None
                ):
                    self._warning_sent = True
                    await self._on_warning("mpv_unavailable", f"mpv unavailable: {exc}")
            finally:
                self._restarting = False

    async def _cleanup_mpv(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self._terminate_process()

    async def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        if self._client is not None:
            try:
                await self._client.command(["quit"])
            except MpvIpcError:
                pass
            await self._client.close()
            self._client = None
        await self._terminate_process()

    async def load(
        self, uri: str, *, start_ms: int = 0, duration_ms: int | None = None
    ) -> None:
        self._uri = uri
        self._duration_hint_ms = duration_ms
        self._last_position_ms = max(start_ms, 0)
        client = self._require_client()
        await self._load_uri(client, uri, start_ms)

    async def play(self) -> None:
        self._desired_playing = True
        await self._require_client().command(["set_property", "pause", False])

    async def pause(self) -> None:
        self._desired_playing = False
        self._last_position_ms = self.get_position_ms()
        await self._require_client().command(["set_property", "pause", True])

    async def stop(self) -> None:
        self._desired_playing = False
        self._last_position_ms = 0
        await self._require_client().command(["stop"])

    async def seek(self, position_ms: int) -> None:
        self._last_position_ms = max(position_ms, 0)
        await self._require_client().command(["seek", position_ms / 1000, "absolute"])

    async def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(100, int(volume)))
        if self._client is not None and self._client.connected:
            await self._client.command(["set_property", "volume", self._volume])

    async def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self._client is not None and self._client.connected:
            await self._client.command(
                ["set_property", "mute", "yes" if self._muted else "no"]
            )

    def get_position_ms(self) -> int:
        if self._client is None:
            return self._last_position_ms
        position = self._client.get_property("time-pos")
        if position is None:
            return self._last_position_ms
        self._last_position_ms = max(int(float(position) * 1000), 0)
        return self._last_position_ms

    def get_duration_ms(self) -> int:
        if self._client is not None:
            duration = self._client.get_property("duration")
            if duration is not None:
                return max(int(float(duration) * 1000), 0)
        return self._duration_hint_ms or 0

    def take_eof(self) -> bool:
        return self._client.take_eof() if self._client is not None else False

    def take_error(self) -> str | None:
        error, self._error = self._error, None
        return error

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "ready": self.ready,
            "pid": self._process.pid if self._process is not None else None,
            "uri": self._uri,
            "muted": self._muted,
            "restarts": self._restart_count,
            "socket": str(self._settings.ipc_socket),
        }

    def _require_client(self) -> MpvIpcClient:
        if self._client is None or not self._client.connected:
            raise MpvIpcError("mpv is not ready")
        return self._client


def find_mpv(path: str) -> str | None:
    """Return the resolved mpv executable, if available."""

    return shutil.which(path)
