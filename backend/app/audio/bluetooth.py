"""Bluetooth adapter boundary. Pairing and A2DP source are P7."""

from __future__ import annotations

from typing import Any, Protocol


class BluetoothAdapter(Protocol):
    async def status(self) -> dict[str, Any]: ...


class MockBluetoothAdapter:
    backend = "mock"

    async def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "adapter": "off",
            "devices": 0,
            "note": "Bluetooth audio is not implemented before P7",
        }
