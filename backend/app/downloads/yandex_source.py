"""Real Yandex file resolver for the P4 download pipeline."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.downloads.base import SourceFile
from app.downloads.quality import select_source_file
from app.errors import NoPlayableSourceError, UnavailableError
from app.integrations.yandex_music.mapper import cover_url
from app.integrations.yandex_music.repository import YandexRepository
from app.logging_setup import get_logger

logger = get_logger("downloads.yandex_source")

LOSSLESS_CODECS = ["flac", "flac-mp4", "aac-mp4", "he-aac-mp4", "mp3"]


class YandexDownloadSource:
    """Resolve one internal track to a concrete Yandex-encoded file.

    The source never transcodes and never persists signed URLs. It prefers
    ``raw`` because the server's ``encraw`` flag was not reliable for FLAC-in-
    MP4 in the real smoke; ``encraw`` is used only when it is the only offer.
    """

    def __init__(
        self,
        settings: Settings,
        auth: Any,
        repository: YandexRepository,
        *,
        cdn: Any | None = None,
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._repository = repository
        self._cdn = cdn

    async def _rewrite(self, url: str) -> str:
        return await self._cdn.rewrite(url) if self._cdn is not None else url

    async def resolve(self, track_id: int) -> SourceFile:
        external_id = await self._repository.get_external_id(track_id)
        if external_id is None:
            raise NoPlayableSourceError(
                "track has no Yandex source",
                details={"track_id": track_id, "reason": "no_online_source"},
            )
        yandex_id = external_id.split(":", 1)[0]
        try:
            client = await self._auth.get_client()
            cover = await self._cover_url(client, yandex_id)
            quality = self._settings.downloads.quality
            candidates: list[SourceFile] = []

            # Ask Yandex for the requested family first. ``tracks_file_info``
            # exposes lossless/hq/nq; ``tracks_download_info`` is the mp3 path.
            if quality in ("auto", "flac"):
                await self._add_file_info(
                    candidates, client, yandex_id, "lossless", cover
                )
            if quality in ("auto", "aac", "flac") and not candidates:
                await self._add_file_info(candidates, client, yandex_id, "hq", cover)
            if quality in ("auto", "aac", "flac") and not candidates:
                await self._add_file_info(candidates, client, yandex_id, "nq", cover)
            if quality == "mp3" or not candidates:
                infos = await client.tracks_download_info(yandex_id, get_direct_links=True)
                for fd in infos or []:
                    url = getattr(fd, "direct_link", None)
                    if not url:
                        url = await fd.get_direct_link_async()
                    if not url:
                        continue
                    candidates.append(
                        SourceFile(
                            url=await self._rewrite(str(url)),
                            codec=str(getattr(fd, "codec", "") or ""),
                            quality=str(getattr(fd, "quality", "") or ""),
                            transport="raw",
                            preview=bool(getattr(fd, "preview", False)),
                            cover_url=cover,
                        )
                    )
            if not candidates:
                raise NoPlayableSourceError(
                    "Yandex returned no downloadable file",
                    details={"track_id": track_id, "reason": "no_file_info"},
                )
            order = {
                "auto": ("flac", "aac", "mp3"),
                "flac": ("flac", "aac", "mp3"),
                "aac": ("aac", "flac", "mp3"),
                "mp3": ("mp3", "aac", "flac"),
            }.get(quality, ("flac", "aac", "mp3"))
            selection = select_source_file(candidates, order)
            logger.info(
                "download source: track=%s requested=%s codec=%s transport=%s "
                "quality=%s fallback=%s",
                track_id,
                quality,
                selection.codec.raw,
                selection.transport,
                selection.actual_quality,
                selection.fell_back,
            )
            return selection.source
        except (NoPlayableSourceError, UnavailableError):
            raise
        except Exception as exc:
            logger.warning(
                "Yandex download resolve failed for %s (%s)",
                track_id,
                type(exc).__name__,
            )
            raise UnavailableError(
                "Yandex Music file is unavailable",
                code="stream_unavailable",
                details={"track_id": track_id},
            ) from None

    async def _add_file_info(
        self,
        candidates: list[SourceFile],
        client: Any,
        yandex_id: str,
        quality: str,
        cover: str | None,
    ) -> None:
        """Append one ``tracks_file_info`` candidate; missing quality is normal."""

        for transport in ("raw", "encraw"):
            try:
                info = await client.tracks_file_info(
                    yandex_id,
                    quality=quality,
                    codecs=LOSSLESS_CODECS,
                    transport=transport,
                )
            except Exception:
                continue
            fd = getattr(info, "download_info", None) if info is not None else None
            url = getattr(fd, "url", None) if fd is not None else None
            if not url:
                continue
            candidates.append(
                SourceFile(
                    url=await self._rewrite(str(url)),
                    codec=str(getattr(fd, "codec", "") or ""),
                    quality=str(getattr(fd, "quality", "") or ""),
                    transport=str(getattr(fd, "transport", "") or transport),
                    key=getattr(fd, "key", None),
                    preview=False,
                    cover_url=cover,
                )
            )
            return

    async def _cover_url(self, client: Any, yandex_id: str) -> str | None:
        try:
            tracks = await client.tracks([yandex_id])
            if not tracks:
                return None
            return cover_url(getattr(tracks[0], "cover_uri", None), 1000)
        except Exception:
            return None
