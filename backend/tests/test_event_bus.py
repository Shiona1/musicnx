"""Event bus: envelope, fan-out and drop-oldest backpressure."""

from __future__ import annotations

import asyncio

from app.core.event_bus import EventBus


async def test_publish_reaches_all_subscribers() -> None:
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()

    await bus.publish("player.state_changed", {"status": "playing"})

    for subscription in (first, second):
        message = subscription.queue.get_nowait()
        assert message["type"] == "player.state_changed"
        assert message["payload"] == {"status": "playing"}
        assert message["ts"].endswith("Z")
    bus.unsubscribe(first)
    assert bus.subscriber_count == 1


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    bus.unsubscribe(subscription)
    await bus.publish("x", {})
    assert subscription.queue.empty()


async def test_critical_track_event_survives_position_overflow() -> None:
    bus = EventBus(max_queue_size=3)
    subscription = bus.subscribe()
    subscription.accepts_position = True
    for index in range(3):
        await bus.publish("player.position", {"position_ms": index})

    await bus.publish("player.track_changed", {"track_id": 42})

    messages = []
    while not subscription.queue.empty():
        messages.append(subscription.queue.get_nowait())
    types = [message["type"] for message in messages]
    assert "player.track_changed" in types
    assert len(messages) == 3
    assert types.count("player.position") == 2


async def test_position_is_dropped_when_queue_is_full_of_critical_events() -> None:
    bus = EventBus(max_queue_size=2)
    subscription = bus.subscribe()
    for index in range(2):
        await bus.publish("queue.updated", {"index": index})

    await bus.publish("player.position", {"position_ms": 100})

    messages = []
    while not subscription.queue.empty():
        messages.append(subscription.queue.get_nowait())
    assert [message["type"] for message in messages] == ["queue.updated"] * 2


async def test_drop_oldest_keeps_capacity() -> None:
    bus = EventBus(max_queue_size=2)
    subscription = bus.subscribe()
    for index in range(5):
        await bus.publish("tick", {"index": index})
    assert subscription.queue.qsize() == 2
    remaining = [subscription.queue.get_nowait()["payload"]["index"] for _ in range(2)]
    assert remaining == [3, 4]


async def test_position_events_only_go_to_visible_subscriptions() -> None:
    bus = EventBus()
    hidden = bus.subscribe()
    visible = bus.subscribe()
    visible.accepts_position = True

    await bus.publish("player.position", {"position_ms": 1000})
    await bus.publish("player.state_changed", {"status": "playing"})

    assert hidden.queue.qsize() == 1
    assert hidden.queue.get_nowait()["type"] == "player.state_changed"
    assert visible.queue.qsize() == 2
    visible.queue.get_nowait()
    visible.queue.get_nowait()
    await bus.publish("player.position", {"position_ms": 1500})
    assert hidden.queue.empty()
    assert visible.queue.qsize() == 1


async def test_envelope_timestamp_format() -> None:
    message = EventBus.envelope("ping", {})
    assert message["ts"][4] == "-" and message["ts"].endswith("Z")
    assert await asyncio.sleep(0) is None
