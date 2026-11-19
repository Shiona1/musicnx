"""In-process pub/sub bus feeding WebSocket clients.

Each subscriber owns a bounded queue. A slow client loses its oldest event
instead of blocking the publisher (ADR-013). Full state is always recoverable
from a snapshot or REST, so losing position events is safe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.core.timeutils import utc_now_iso

DEFAULT_MAX_QUEUE_SIZE = 256


@dataclass(eq=False)
class Subscription:
    """One WebSocket subscriber's bounded event queue."""

    maxsize: int = DEFAULT_MAX_QUEUE_SIZE
    accepts_position: bool = False
    queue: asyncio.Queue[dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.maxsize)


class EventBus:
    """Fan-out event bus with drop-oldest backpressure."""

    def __init__(self, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscription:
        subscription = Subscription(maxsize=self._max_queue_size)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        message = self.envelope(event_type, payload)
        for subscription in list(self._subscribers):
            # Hidden tabs must not accumulate 2 Hz position events (ADR-007).
            if event_type == "player.position" and not subscription.accepts_position:
                continue
            queue = subscription.queue
            if queue.full():
                # Positions are disposable; state/track/queue events are not.
                dropped_position = self._drop_oldest_position(subscription)
                if not dropped_position and event_type == "player.position":
                    continue
                if not dropped_position:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - race guard
                        pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover - race guard
                pass

    @staticmethod
    def _drop_oldest_position(subscription: Subscription) -> bool:
        """Remove the oldest queued position while preserving all other events."""

        queue = subscription.queue
        items: list[dict[str, Any]] = []
        removed = False
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not removed and item["type"] == "player.position":
                removed = True
                continue
            items.append(item)
        for item in items:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - queue shrank
                break
        return removed

    @staticmethod
    def envelope(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": event_type, "ts": utc_now_iso(), "payload": payload}
