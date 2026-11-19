"""Migration runner: ordered SQL files, recorded in ``schema_migrations``."""

from __future__ import annotations

import re
from pathlib import Path

from app.database.connection import Database
from app.logging_setup import get_logger

logger = get_logger("database.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def _statements(script: str) -> list[str]:
    """Split a SQL script into statements, respecting quotes and comments.

    Migrations must stay trigger-free for this simple splitter; that is enough
    for schema DDL and keeps the runner dependency-free.
    """

    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(script)
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False

    while index < length:
        char = script[index]
        next_char = script[index + 1] if index + 1 < length else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                buffer.append("\n")
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_single:
            buffer.append(char)
            if char == "'" and next_char == "'":
                buffer.append(next_char)
                index += 2
                continue
            if char == "'":
                in_single = False
            index += 1
            continue
        if in_double:
            buffer.append(char)
            if char == '"':
                in_double = False
            index += 1
            continue

        if char == "-" and next_char == "-":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        if char == "'":
            in_single = True
            buffer.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            buffer.append(char)
            index += 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


async def apply_migrations(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply pending migrations in order; return the resulting schema version."""

    await db.connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    await db.connection.commit()

    applied = {
        int(row["version"])
        for row in await db.fetchall("SELECT version FROM schema_migrations")
    }
    files = sorted(migrations_dir.glob("*.sql"))
    versions = 0
    for path in files:
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise RuntimeError(f"invalid migration filename: {path.name}")
        version = int(match.group(1))
        name = match.group(2)
        if version in applied:
            continue
        logger.info("applying migration %s", path.name)
        script = path.read_text(encoding="utf-8")
        async with db.transaction() as conn:
            for statement in _statements(script):
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
        applied.add(version)
        versions = version
    return max(applied) if applied else versions
