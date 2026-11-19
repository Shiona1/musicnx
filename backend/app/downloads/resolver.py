"""DB-backed album/playlist resolvers for the download manager."""

from __future__ import annotations

from app.database.connection import Database


class DownloadResolver:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def album_track_ids(self, album_id: int) -> list[int] | None:
        rows = await self._db.fetchall(
            "SELECT id FROM tracks WHERE album_id = ? ORDER BY id", (album_id,)
        )
        if not rows:
            return None
        return [int(row["id"]) for row in rows]

    async def playlist_track_ids(self, playlist_id: str) -> list[int] | None:
        row = await self._db.fetchone(
            "SELECT id FROM playlists WHERE external_id = ? ORDER BY id LIMIT 1",
            (playlist_id,),
        )
        if row is None:
            return None
        playlist_db_id = int(row["id"])
        rows = await self._db.fetchall(
            "SELECT track_id FROM playlist_tracks "
            "WHERE playlist_id = ? AND removed_at IS NULL ORDER BY position, id",
            (playlist_db_id,),
        )
        return [int(item["track_id"]) for item in rows]
