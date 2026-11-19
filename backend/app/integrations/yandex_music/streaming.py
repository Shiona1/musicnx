"""Resolve short-lived Yandex Music stream URLs (no transcoding, no buffering).

A direct link expires quickly, so it is resolved immediately before playback.
Callers that receive HTTP 403/410 from the CDN ask again with
``force_refresh=True``: the auth manager hands out a fresh token and a new signed
link (docs/YANDEX_API.md §3, docs/API.md §6.4).
"""

from __future__ import annotations

from app.config import Settings
from app.errors import NoPlayableSourceError, UnavailableError
from app.integrations.yandex_music.base import YandexStreamInfo
from app.integrations.yandex_music.client import YandexClient
from app.integrations.yandex_music.mapper import (
    NormalizedDownloadInfo,
    direct_link_expiry,
    map_download_infos,
    select_download_info,
)
from app.integrations.yandex_music.repository import YandexRepository
from app.logging_setup import get_logger

logger = get_logger("yandex.streaming")

StreamRef = int | str


class YandexStreamingService:
    """Pick a quality, resolve the direct link and describe the stream."""

    def __init__(
        self,
        client: YandexClient,
        repository: YandexRepository,
        settings: Settings,
    ) -> None:
        self._client = client
        self._repository = repository
        self._settings = settings

    async def stream_info(
        self, track_ref: StreamRef, *, force_refresh: bool = False
    ) -> YandexStreamInfo:
        """Return a playable URL for an internal track id or external id."""

        external_id = await self._external_id(track_ref)
        yandex_id = self._yandex_track_id(external_id)

        raw_infos = await self._client.download_infos(
            yandex_id, refresh=force_refresh
        )
        pairs: list[tuple[object, NormalizedDownloadInfo]] = []
        for raw in raw_infos:
            mapped = map_download_infos([raw])
            if mapped:
                pairs.append((raw, mapped[0]))
        chosen = select_download_info(
            [normalized for _, normalized in pairs],
            self._settings.yandex.quality,
        )
        if chosen is None:
            raise UnavailableError(
                "Yandex Music returned no stream for this track",
                code="stream_unavailable",
                details={"reason": "no_download_info", "external_id": external_id[:64]},
            )

        url = chosen.direct
        if url is None or force_refresh:
            raw_chosen = next(raw for raw, normalized in pairs if normalized is chosen)
            url = await self._client.direct_link(raw_chosen, refresh=force_refresh)

        return YandexStreamInfo(
            url=url,
            codec=chosen.codec,
            bitrate_kbps=chosen.bitrate_kbps,
            preview=chosen.preview,
            expires_at=direct_link_expiry(url),
        )

    async def _external_id(self, track_ref: StreamRef) -> str:
        if isinstance(track_ref, int):
            external_id = await self._repository.get_external_id(track_ref)
            if external_id is None:
                raise NoPlayableSourceError(
                    "track has no Yandex Music source",
                    details={"track_id": track_ref, "reason": "no_online_source"},
                )
            return external_id

        value = str(track_ref).strip()
        if not value:
            raise NoPlayableSourceError(
                "empty track reference",
                details={"reason": "invalid_external_id"},
            )
        if value.isdigit():
            # Routes receive internal ids; accept the string form as well.
            return await self._external_id(int(value))
        return value

    @staticmethod
    def _yandex_track_id(external_id: str) -> str:
        head = external_id.split(":", 1)[0]
        if not head.isdigit():
            raise NoPlayableSourceError(
                "invalid Yandex Music external id",
                details={"external_id": external_id[:64], "reason": "invalid_external_id"},
            )
        return head
