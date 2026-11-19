"""Artwork cache: sizes, deterministic keys, LRU and temp cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from app.core.event_bus import EventBus
from app.core.models import LocalMediaMetadata
from app.database.connection import Database
from app.database.migrations import apply_migrations
from app.database.repositories import LibraryRepository
from app.library.covers import CoverCache
from app.library.importer import ImportManager
from PIL import Image, UnidentifiedImageError
from tests.conftest import make_settings


def _image_bytes(seed: int) -> bytes:
    image = Image.new("RGB", (600, 600), (seed % 255, (seed * 3) % 255, (seed * 7) % 255))
    for index in range(0, 600, 4):
        for row in range(0, 600, 4):
            image.putpixel((index, row), ((index * seed) % 255, (row * seed) % 255, 128))
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


async def test_store_renders_all_sizes_and_is_deterministic(tmp_path: Path) -> None:
    cache = CoverCache(tmp_path / "covers", max_bytes=10_000_000, sizes=[300, 1000, 64])
    data = _image_bytes(1)
    key, evicted = await cache.store(data)
    assert evicted == []
    same_key, same_evicted = await cache.store(data)
    assert same_key == key
    assert same_evicted == []
    for size in (300, 1000, 64):
        path = cache.path_for(key, size)
        assert path.is_file()
        with Image.open(path) as image:
            assert image.size == (size, size)
    # 64px thumbnail is pre-blurred: JPEG output differs from a plain resize.
    assert cache.path_for(key, 64).stat().st_size < cache.path_for(key, 300).stat().st_size


async def test_lru_limit_deletes_oldest(tmp_path: Path) -> None:
    cache = CoverCache(tmp_path / "covers", max_bytes=520_000, sizes=[300, 1000, 64])
    keys = []
    evicted: list[str] = []
    for seed in range(1, 6):
        key, gone = await cache.store(_image_bytes(seed))
        keys.append(key)
        evicted.extend(gone)
        time.sleep(0.01)
    total = sum(path.stat().st_size for path in (tmp_path / "covers").glob("*.jpg"))
    assert total <= 520_000
    assert keys[0] in evicted
    assert not cache.path_for(keys[0], 300).is_file()
    assert cache.path_for(keys[-1], 300).is_file()


async def test_temp_cleanup(tmp_path: Path) -> None:
    cache = CoverCache(tmp_path / "covers", max_bytes=10_000_000, sizes=[300])
    cache.ensure_dir()
    stale = cache.root / "stale.123.tmp"
    stale.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    await cache.store(_image_bytes(1))
    assert not stale.exists()


async def test_invalid_image_raises(tmp_path: Path) -> None:
    cache = CoverCache(tmp_path / "covers", max_bytes=10_000_000, sizes=[300])
    with pytest.raises(UnidentifiedImageError):
        await cache.store(b"not an image")


def _metadata(path: Path, album: str) -> LocalMediaMetadata:
    return LocalMediaMetadata(
        path=path,
        relative_path=path.name,
        format="mp3",
        title="Cover Test",
        artists=["Test Artist"],
        album=album,
        albumartist="Test Artist",
        year=2024,
        genre="Test",
        track_number=1,
        disc_number=1,
        label="Test Label",
        duration_ms=3000,
        size_bytes=1024,
    )


async def test_store_cover_rerenders_and_clears_evicted_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, seed_catalog=False)
    database = Database(settings.paths.database)
    await database.connect()
    try:
        await apply_migrations(database)
        library = LibraryRepository(database)
        cache = CoverCache(
            tmp_path / "covers", max_bytes=10_000_000, sizes=[300, 1000, 64]
        )
        importer = ImportManager(
            settings=settings, bus=EventBus(), library=library, covers=cache
        )
        source = await library.get_or_create_source("local")
        first = await library.import_local(_metadata(tmp_path / "one.mp3", "Album One"), source)
        image_one = _image_bytes(1)
        await importer._store_cover(first, image_one)
        key_one = await library.album_cover(first.album_id or 0)
        assert key_one is not None
        assert cache.path_for(key_one, 300).is_file()

        # A stale albums.cover_path must be re-rendered when its cache file is gone.
        cache.path_for(key_one, 300).unlink()
        await importer._store_cover(first, image_one)
        assert cache.path_for(key_one, 300).is_file()

        second = await library.import_local(_metadata(tmp_path / "two.mp3", "Album Two"), source)
        image_two = _image_bytes(2)
        probe = CoverCache(
            tmp_path / "probe", max_bytes=10_000_000, sizes=[300, 1000, 64]
        )
        key_two, _ = await probe.store(image_two)
        size_one = sum(cache.path_for(key_one, size).stat().st_size for size in cache.sizes)
        size_two = sum(probe.path_for(key_two, size).stat().st_size for size in probe.sizes)
        # Leave room for the new key but not for both cover sets.
        cache.max_bytes = max(size_one, size_two)

        await importer._store_cover(second, image_two)
        assert await library.album_cover(first.album_id or 0) is None
        assert await library.album_cover(second.album_id or 0) == key_two
    finally:
        await database.close()
