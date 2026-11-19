"""Storage information for ``GET /system/storage``."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # pragma: no cover - hostile filesystem
        return False


async def storage_payload(
    settings: Settings, database_path: Path, database_size: int
) -> dict[str, Any]:
    paths = settings.paths
    probe = paths.data_dir if paths.data_dir.exists() else Path.cwd()
    disk = await asyncio.to_thread(shutil.disk_usage, probe)
    return {
        "paths": {
            "data": str(paths.data_dir),
            "database": str(paths.database),
            "library": str(paths.library),
            "downloads": str(paths.downloads),
            "cache": str(paths.cache),
        },
        "exists": {
            "database": _exists(database_path),
            "library": _exists(paths.library),
            "downloads": _exists(paths.downloads),
            "cache": _exists(paths.cache),
        },
        "database_size_bytes": database_size,
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
    }
