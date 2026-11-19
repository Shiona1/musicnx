"""mpv JSON IPC over a unix socket.

The transport is injectable so framing, request correlation and property events
are tested without launching mpv (AGENTS.md §4). Only POSIX is supported in P1
(ADR-006).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

from app.logging_setup import get_logger

logger = get_logger("audio.mpv")

OBSERVED_PROPERTIES: tuple[tuple[int, str], ...] = (
    (1, "time-pos"),
    (2, "duration"),
    (3, "idle-active"),
    (4, "eof-reached"),
)

COMMAND_TIMEOUT_S = 5.0


class MpvIpcError(RuntimeError):
    """IPC-level failure (not connected, closed, timeout)."""


class MpvCommandError(MpvIpcError):
    """mpv answered a command with a non-success error string."""

    def __init__(self, command: list[Any], error: str) -> None:
        self.command = command
        self.mpv_error = error
        super().__init__(f"mpv command {command!r} failed: {error}")


class MpvTransport(Protocol):
    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]: ...

    async def close(self) -> None: ...


class UnixSocketTransport:
    """Connect to an existing mpv unix socket, retrying until mpv creates it."""

    def __init__(self, path: Path, timeout_s: float = 10.0) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        last_error: Exception | None = None
        while loop.time() < deadline:
            try:
                reader, writer = await asyncio.open_unix_connection(self._path)
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_error = exc
                await asyncio.sleep(0.1)
                continue
            self._writer = writer
            return reader, writer
        raise MpvIpcError(f"cannot connect to mpv IPC socket {self._path}: {last_error}")

    async def close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):  # pragma: no cover - close races
            pass
        self._writer = None


class MpvIpcClient:
    """Line-delimited JSON client with request/response correlation."""

    def __init__(self, transport: MpvTransport) -> None:
        self._transport = transport
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._commands: dict[int, list[Any]] = {}
        self._next_request_id = 1
        self._eof = asyncio.Event()
        self._file_loaded = asyncio.Event()
        self._closed = True

    @property
    def connected(self) -> bool:
        return not self._closed and self._writer is not None

    @property
    def properties(self) -> dict[str, Any]:
        return self._properties

    async def connect(self) -> None:
        reader, writer = await self._transport.connect()
        self._reader = reader
        self._writer = writer
        self._properties: dict[str, Any] = {}
        self._closed = False
        self._read_task = asyncio.create_task(self._read_loop(), name="mpv-ipc-reader")
        for observer_id, name in OBSERVED_PROPERTIES:
            try:
                await self.command(["observe_property", observer_id, name])
            except MpvIpcError as exc:  # pragma: no cover - depends on mpv
                logger.warning("cannot observe %s: %s", name, exc)

    async def command(self, command: list[Any], *, timeout_s: float = COMMAND_TIMEOUT_S) -> Any:
        if self._writer is None or self._closed:
            raise MpvIpcError("mpv IPC is not connected")
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._commands[request_id] = command
        payload = json.dumps({"command": command, "request_id": request_id}) + "\n"
        self._writer.write(payload.encode("utf-8"))
        await self._writer.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError as exc:
            raise MpvIpcError(f"mpv command timed out: {command!r}") from exc
        finally:
            self._pending.pop(request_id, None)
            self._commands.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("invalid JSON from mpv: %r", line[:200])
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            logger.warning("mpv IPC read loop failed: %s", exc)
        finally:
            self._closed = True
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(MpvIpcError("mpv IPC connection closed"))
            self._pending.clear()

    def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        if request_id is not None:
            future = self._pending.get(int(request_id))
            if future is not None and not future.done():
                error = message.get("error", "success")
                if error == "success":
                    future.set_result(message.get("data"))
                else:
                    future.set_exception(
                        MpvCommandError(self._commands.get(int(request_id), []), str(error))
                    )
            return

        event = message.get("event")
        if event == "property-change":
            name = message.get("name")
            if name:
                value = message.get("data")
                self._properties[name] = value
                if name == "eof-reached" and value is True:
                    self._eof.set()
        elif event == "end-file":
            # Replacing a file also emits end-file (reason "stop"); only a real
            # EOF should advance the queue.
            if message.get("reason") == "eof":
                self._eof.set()
        elif event == "file-loaded":
            self._file_loaded.set()

    def take_eof(self) -> bool:
        if not self._eof.is_set():
            return False
        self._eof.clear()
        return True

    async def wait_for_file_loaded(self, *, timeout_s: float = 10.0) -> None:
        """Wait for mpv's ``file-loaded`` event.

        ``loadfile`` returns before the file is seekable; seeking immediately
        after it fails with ``error running command``. The event is the exact
        signal that metadata and demuxer are ready.
        """

        try:
            await asyncio.wait_for(self._file_loaded.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise MpvIpcError("timed out waiting for mpv file-loaded") from exc

    def reset_file_loaded(self) -> None:
        self._file_loaded.clear()
        self._eof.clear()

    def get_property(self, name: str) -> Any:
        return self._properties.get(name)

    async def close(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        await self._transport.close()
        self._writer = None
        self._reader = None
