"""Queue operations on top of the persistent queue repository."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.models import QueueItem, QueueState
from app.database.repositories import QueueRepository, TrackRepository
from app.errors import NotFoundError


class QueueManager:
    """Validates track existence and builds queue snapshots.

    The queue itself lives in SQLite (``queue_items``): no in-memory copy means
    persistence and restore cannot drift from what the API reports.
    """

    def __init__(self, repository: QueueRepository, tracks: TrackRepository) -> None:
        self._repository = repository
        self._tracks = tracks

    async def items(self) -> list[QueueItem]:
        return await self._repository.all()

    async def item(self, item_id: int) -> QueueItem | None:
        return await self._repository.get(item_id)

    async def count(self) -> int:
        return await self._repository.count()

    async def add_tracks(self, track_ids: Sequence[int]) -> list[int]:
        missing = [track_id for track_id in track_ids if not await self._tracks.exists(track_id)]
        if missing:
            raise NotFoundError(
                "one or more tracks do not exist",
                details={"track_ids": missing},
            )
        return await self._repository.add_many(track_ids)

    async def remove_item(self, item_id: int) -> QueueItem:
        item = await self._repository.get(item_id)
        if item is None:
            raise NotFoundError("queue item not found", details={"item_id": item_id})
        if not await self._repository.remove(item_id):
            raise NotFoundError("queue item not found", details={"item_id": item_id})
        return item

    async def clear_except_current(self, item_id: int | None) -> int:
        """Drop every queued item except the current one (None clears all)."""

        return await self._repository.clear_except(item_id)

    async def reorder(self, item_ids: Sequence[int]) -> None:
        await self._repository.reorder(item_ids)

    async def replace_track(self, item_id: int, track_id: int) -> QueueItem:
        """Replace the track behind one queue item (used by source fallback)."""

        if not await self._tracks.exists(track_id):
            raise NotFoundError(
                "track does not exist", details={"track_id": track_id}
            )
        if not await self._repository.replace_track(item_id, track_id):
            raise NotFoundError("queue item not found", details={"item_id": item_id})
        item = await self._repository.get(item_id)
        if item is None:  # pragma: no cover - updated row always exists
            raise NotFoundError("queue item not found", details={"item_id": item_id})
        return item

    async def find_item_id_by_track(self, track_id: int) -> int | None:
        return await self._repository.find_item_id_by_track(track_id)

    async def snapshot(self, current_item_id: int | None) -> QueueState:
        items = await self._repository.all()
        current_index = None
        if current_item_id is not None:
            for index, item in enumerate(items):
                if item.id == current_item_id:
                    current_index = index
                    break
        total_duration_ms = sum(item.track.duration_ms or 0 for item in items)
        return QueueState(
            items=items,
            current_item_id=current_item_id,
            current_index=current_index,
            total=len(items),
            total_duration_ms=total_duration_ms,
        )
