"""Disk-backed artwork cache: 300/1000/64, pre-blurred thumbnail, LRU limit."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from app.logging_setup import get_logger

logger = get_logger("library.covers")

ALLOWED_SIZES = frozenset({64, 300, 1000})
_TEMP_SUFFIX = ".tmp"
_TEMP_MAX_AGE_S = 3600


class CoverCache:
    def __init__(self, root: Path, *, max_bytes: int, sizes: list[int]) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.sizes = [size for size in sizes if size in ALLOWED_SIZES] or [300]
        self._lock = asyncio.Lock()

    def ensure_dir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:32]

    def path_for(self, key: str, size: int) -> Path:
        return self.root / f"{key}-{size}.jpg"

    def exists(self, key: str, size: int) -> bool:
        return self.path_for(key, size).is_file()

    async def store(self, data: bytes) -> tuple[str, list[str]]:
        """Render all sizes and return ``(key, evicted_keys)``.

        Evicted keys no longer have files on disk; callers must clear any
        database references to them (see ``LibraryRepository.clear_cover_key``).
        """

        key = self.key_for(data)
        async with self._lock:
            await asyncio.to_thread(self._render_all, key, data)
            await asyncio.to_thread(self._cleanup_temp)
            evicted = await asyncio.to_thread(self._enforce_limit)
        return key, evicted

    def _render_all(self, key: str, data: bytes) -> None:
        self.ensure_dir()
        try:
            with Image.open(io.BytesIO(data)) as source:
                source = ImageOps.exif_transpose(source)
                source = source.convert("RGB")
                for size in self.sizes:
                    target_path = self.path_for(key, size)
                    if target_path.is_file():
                        continue
                    image = ImageOps.fit(source, (size, size), method=Image.Resampling.LANCZOS)
                    if size == 64:
                        image = image.filter(ImageFilter.GaussianBlur(12))
                    temp_path = target_path.with_suffix(f".{os.getpid()}{_TEMP_SUFFIX}")
                    image.save(temp_path, format="JPEG", quality=86, optimize=True)
                    os.replace(temp_path, target_path)
        except (OSError, ValueError) as exc:
            logger.warning("cannot render cover %s: %s", key, exc)
            raise

    def _cleanup_temp(self) -> None:
        cutoff = time.time() - _TEMP_MAX_AGE_S
        for path in self.root.glob(f"*{_TEMP_SUFFIX}"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _enforce_limit(self) -> list[str]:
        files = []
        try:
            entries = list(self.root.glob("*.jpg"))
        except OSError:
            return []
        for path in entries:
            try:
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        total = sum(item[1] for item in files)
        if total <= self.max_bytes:
            return []
        files.sort(key=lambda item: item[0])
        evicted: set[str] = set()
        for _, size, path in files:
            if total <= self.max_bytes:
                break
            key = self._key_from_path(path)
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
            if key is not None:
                evicted.add(key)
        return sorted(evicted)

    @staticmethod
    def _key_from_path(path: Path) -> str | None:
        name = path.name
        if not name.endswith(".jpg"):
            return None
        key, separator, size = name[: -len(".jpg")].rpartition("-")
        if not separator or not key or not size.isdigit():
            return None
        return key
