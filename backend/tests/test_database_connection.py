"""SQLite connection invariants (regression for implicit transactions)."""

from __future__ import annotations

from app.database.connection import Database


async def test_ad_hoc_write_does_not_leave_an_implicit_transaction(db: Database) -> None:
    # Migrations and diagnostics use the connection outside ``transaction()``.
    # With the old default isolation level that left an implicit transaction
    # open and the next ``BEGIN IMMEDIATE`` failed during shutdown.
    await db.connection.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('test.autocommit', '1')"
    )
    assert db.connection.in_transaction is False

    async with db.transaction() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('test.autocommit', '2')"
        )

    value = await db.scalar("SELECT value FROM settings WHERE key = 'test.autocommit'")
    assert value == "2"
