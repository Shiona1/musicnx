"""mpv JSON IPC framing and correlation with a fake transport (no mpv)."""

from __future__ import annotations

import asyncio
import json

import pytest
from app.audio.mpv_ipc import (
    OBSERVED_PROPERTIES,
    MpvCommandError,
    MpvIpcClient,
    MpvIpcError,
)


class FakeWriter:
    def __init__(self, transport: FakeTransport) -> None:
        self._transport = transport
        self.closed = False

    def write(self, data: bytes) -> None:
        self._transport.handle_write(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeTransport:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = FakeWriter(self)
        self.commands: list[list[object]] = []
        self.fail_commands: set[str] = set()
        self.silent_commands: set[str] = set()
        self.closed = False

    async def connect(self) -> tuple[asyncio.StreamReader, FakeWriter]:
        return self.reader, self.writer

    async def close(self) -> None:
        self.closed = True

    def feed(self, message: dict) -> None:
        self.reader.feed_data((json.dumps(message) + "\n").encode())

    def handle_write(self, data: bytes) -> None:
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            command = message["command"]
            request_id = message["request_id"]
            self.commands.append(command)
            name = command[0]
            if name in self.silent_commands:
                continue
            if name in self.fail_commands:
                self.feed(
                    {"request_id": request_id, "error": "loading failed", "data": None}
                )
            elif name == "get_property":
                self.feed({"request_id": request_id, "error": "success", "data": 42})
            else:
                self.feed({"request_id": request_id, "error": "success", "data": None})


async def test_connect_observes_required_properties() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        observed = [
            command[2]
            for command in transport.commands
            if command[0] == "observe_property"
        ]
        assert observed == [name for _, name in OBSERVED_PROPERTIES]
    finally:
        await client.close()


async def test_command_response_correlation() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        result = await client.command(["get_property", "volume"])
        assert result == 42
        assert transport.commands[-1] == ["get_property", "volume"]
    finally:
        await client.close()


async def test_property_events_update_cache() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        transport.feed(
            {
                "event": "property-change",
                "id": 1,
                "name": "time-pos",
                "data": 12.5,
            }
        )
        await asyncio.sleep(0)
        assert client.get_property("time-pos") == 12.5
        assert client.get_property("duration") is None
    finally:
        await client.close()


async def test_eof_flag_is_consumed_once() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        transport.feed(
            {"event": "property-change", "id": 4, "name": "eof-reached", "data": True}
        )
        await asyncio.sleep(0)
        assert client.take_eof() is True
        assert client.take_eof() is False
    finally:
        await client.close()


async def test_command_error_is_raised() -> None:
    transport = FakeTransport()
    transport.fail_commands.add("loadfile")
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        with pytest.raises(MpvCommandError) as error:
            await client.command(["loadfile", "/tmp/a.flac"])
        assert "loading failed" in str(error.value)
    finally:
        await client.close()


async def test_command_timeout() -> None:
    transport = FakeTransport()
    transport.silent_commands.add("silent")
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        with pytest.raises(MpvIpcError, match="timed out"):
            await client.command(["silent"], timeout_s=0.05)
    finally:
        await client.close()


async def test_eof_end_file_requires_reason_eof() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        transport.feed({"event": "end-file", "reason": "stop"})
        await asyncio.sleep(0)
        assert client.take_eof() is False

        transport.feed({"event": "end-file", "reason": "eof"})
        await asyncio.sleep(0)
        assert client.take_eof() is True
    finally:
        await client.close()


async def test_wait_for_file_loaded_returns_on_event() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        client.reset_file_loaded()
        waiter = asyncio.create_task(client.wait_for_file_loaded(timeout_s=1.0))
        transport.feed({"event": "file-loaded"})
        await asyncio.wait_for(waiter, timeout=1.0)
    finally:
        await client.close()


async def test_wait_for_file_loaded_times_out() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    try:
        client.reset_file_loaded()
        with pytest.raises(MpvIpcError, match="timed out"):
            await client.wait_for_file_loaded(timeout_s=0.05)
    finally:
        await client.close()


async def test_command_after_close_fails() -> None:
    transport = FakeTransport()
    client = MpvIpcClient(transport)
    await client.connect()
    await client.close()
    assert client.connected is False
    with pytest.raises(MpvIpcError):
        await client.command(["get_property", "volume"])
