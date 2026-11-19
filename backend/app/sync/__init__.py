"""P6 catalog sync: run history persistence and the single-flight sync engine.

The package owns no HTTP or Yandex I/O: :class:`~app.sync.service.SyncService`
drives the frozen :class:`~app.integrations.yandex_music.base.YandexAdapter`
contract and stores runs through :class:`~app.sync.repository.SyncRepository`.
"""

from __future__ import annotations

from app.sync.repository import SyncRepository, row_to_sync_run
from app.sync.service import SyncService

__all__ = ["SyncRepository", "SyncService", "row_to_sync_run"]
