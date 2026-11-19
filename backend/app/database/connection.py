"""SQLite connection with WAL and a single write lock."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

Row = aiosqlite.Row


class Database:
    """One aiosqlite connection per process, serialised writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        # Autocommit mode: writes outside ``transaction()`` (migrations,
        # ad-hoc diagnostics) must not leave an implicit transaction open,
        # otherwise the next explicit ``BEGIN IMMEDIATE`` fails with
        # "cannot start a transaction within a transaction" — especially
        # during shutdown, when the scheduler requeues a cancelled job.
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA temp_store = MEMORY")
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is None:
            return
        async with self._write_lock:
            await self._conn.close()
            self._conn = None

    async def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list[Row]:
        cursor = await self.connection.execute(sql, parameters)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> Row | None:
        cursor = await self.connection.execute(sql, parameters)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def scalar(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        row = await self.fetchone(sql, parameters)
        return row[0] if row is not None else None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialise a write transaction; nested transactions are forbidden."""

        async with self._write_lock:
            conn = self.connection
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def schema_version(self) -> int:
        row = await self.fetchone("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        return int(row[0]) if row is not None else 0

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
