"""Network adapter boundary. Real NetworkManager integration is P8."""

from __future__ import annotations

from typing import Any, Protocol


class NetworkAdapter(Protocol):
    async def status(self) -> dict[str, Any]: ...


class MockNetworkAdapter:
    """Deterministic LAN status for P0/P1 development and tests."""

    backend = "mock"

    async def status(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "connected": True,
            "interface": "mock0",
            "note": "NetworkManager integration is not implemented before P8",
        }
