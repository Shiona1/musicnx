"""Seed data and queue manager persistence."""

from __future__ import annotations

import pytest
from app.core.queue_manager import QueueManager
from app.database.connection import Database
from app.database.repositories import QueueRepository, TrackRepository
from app.database.seed import seed_mock_catalog
from app.errors import NotFoundError, QueueChangedError
from fastapi.testclient import TestClient


async def test_seed_is_idempotent(db: Database) -> None:
    async def counts() -> tuple[int, int, int, int]:
        tracks = await db.scalar("SELECT COUNT(*) FROM tracks")
        artists = await db.scalar("SELECT COUNT(*) FROM artists")
        playlist_tracks = await db.scalar("SELECT COUNT(*) FROM playlist_tracks")
        playlist_sources = await db.scalar("SELECT COUNT(*) FROM sources")
        return int(tracks), int(artists), int(playlist_tracks), int(playlist_sources)

    first = await counts()
    await seed_mock_catalog(db)
    await seed_mock_catalog(db)
    assert await counts() == first
    assert first[0] == 6


async def test_queue_manager_add_remove_reorder(db: Database) -> None:
    tracks = TrackRepository(db)
    manager = QueueManager(QueueRepository(db), tracks)
    track_ids = [track.id for track in (await tracks.list(limit=3))[0]]

    created = await manager.add_tracks(track_ids)
    assert len(created) == 3
    snapshot = await manager.snapshot(current_item_id=created[1])
    assert snapshot.total == 3
    assert snapshot.current_index == 1
    assert snapshot.total_duration_ms > 0
    assert [item.position for item in snapshot.items] == [0, 1, 2]

    await manager.reorder([created[2], created[0], created[1]])
    snapshot = await manager.snapshot(current_item_id=created[1])
    assert [item.id for item in snapshot.items] == [created[2], created[0], created[1]]
    assert snapshot.current_index == 2

    with pytest.raises(QueueChangedError):
        await manager.reorder([created[0], created[1]])

    removed = await manager.remove_item(created[0])
    assert removed.id == created[0]
    snapshot = await manager.snapshot(current_item_id=created[1])
    assert [item.id for item in snapshot.items] == [created[2], created[1]]
    assert [item.position for item in snapshot.items] == [0, 1]
    assert snapshot.current_index == 1


async def test_queue_manager_rejects_unknown_tracks(db: Database) -> None:
    manager = QueueManager(QueueRepository(db), TrackRepository(db))
    with pytest.raises(NotFoundError):
        await manager.add_tracks([999_999])


async def test_queue_manager_remove_unknown_item(db: Database) -> None:
    manager = QueueManager(QueueRepository(db), TrackRepository(db))
    with pytest.raises(NotFoundError):
        await manager.remove_item(999_999)


def test_seed_catalog_visible_through_api(client: TestClient) -> None:
    payload = client.get("/api/v1/library/tracks").json()
    titles = {item["title"] for item in payload["items"]}
    assert "Ночной эфир" in titles
    assert "Короткая проверка" in titles
    assert all(item["availability"] == "online" for item in payload["items"])
