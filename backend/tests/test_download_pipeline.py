"""P4 download pipeline tests: fake source/HTTP, encraw, tags, paths, resume.

No test opens a socket, touches Yandex Music or needs a Raspberry Pi. The
``FakeSession`` mimics the small aiohttp surface the pipeline uses.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import mutagen
import pytest
from app.config import Settings
from app.core.models import DownloadTask
from app.downloads.base import SourceFile
from app.downloads.decrypt import (
    AesCtrDecryptor,
    DecryptError,
    decrypt_bytes,
    validate_key,
)
from app.downloads.integrity import (
    IntegrityError,
    sha256_file,
    sniff_bytes,
    verify_file,
)
from app.downloads.paths import PathTemplateError
from app.downloads.pipeline import DownloadHttpError, DownloadPipeline
from app.downloads.quality import (
    DEFAULT_QUALITY_ORDER,
    classify_codec,
    describe_source,
    normalize_quality_order,
    select_source_file,
)
from app.downloads.tags import TrackTags, write_tags
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from multidict import CIMultiDict

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "media"
TAGS = TrackTags(
    title="Ночной эфир",
    artists=("Тестовый Оркестр", "Второй Артист"),
    album="Mock Album",
    albumartist="Тестовый Оркестр",
    year=2024,
    genre="Electronic",
    track_number=1,
    disc_number=1,
    label="Mock Label",
)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_task(task_id: int = 7, track_id: int = 501) -> DownloadTask:
    return DownloadTask(
        id=task_id,
        track_id=track_id,
        title="Track",
        status="downloading",
        created_at="2026-11-14T18:00:00+00:00",
        updated_at="2026-11-14T18:00:00+00:00",
    )


class FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, chunk_size: int):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


class FakeResponse:
    def __init__(
        self, body: bytes = b"", *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = CIMultiDict(headers or {})
        self.content = FakeContent(body)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class FakeSession:
    """Async-context-manager stand-in for ``aiohttp.ClientSession``."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        return self._handler(url, kwargs)

    async def close(self) -> None:
        self.closed = True


class FakeSource:
    def __init__(self, source: SourceFile) -> None:
        self.source = source
        self.resolved: list[int] = []

    async def resolve(self, track_id: int) -> SourceFile:
        self.resolved.append(track_id)
        return self.source


class Progress:
    def __init__(self) -> None:
        self.events: list[tuple[int, int | None]] = []

    async def __call__(self, done: int, total: int | None) -> None:
        self.events.append((done, total))


def serve_body(body: bytes, *, support_range: bool = True):
    def handler(url: str, kwargs: dict[str, Any]) -> FakeResponse:
        headers = kwargs.get("headers") or {}
        range_header = headers.get("Range")
        if range_header and support_range:
            start = int(range_header.removeprefix("bytes=").rstrip("-"))
            if start >= len(body):
                return FakeResponse(status=416)
            remaining = body[start:]
            return FakeResponse(
                remaining,
                status=206,
                headers={
                    "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                    "Content-Length": str(len(remaining)),
                },
            )
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    return handler


def make_pipeline(
    settings: Settings,
    source: FakeSource,
    session: FakeSession,
    *,
    metadata: TrackTags | None = TAGS,
    **kwargs: Any,
) -> DownloadPipeline:
    async def provider(track_id: int) -> TrackTags | None:
        return metadata

    return DownloadPipeline(
        settings,
        source,
        metadata_provider=provider,
        session=session,
        **kwargs,
    )


def encrypt_ctr(data: bytes, key: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.CTR(bytes(16))).encryptor()
    return encryptor.update(data) + encryptor.finalize()


# ---------------------------------------------------------------- decrypt unit


def test_decrypt_bytes_roundtrip_with_cryptography() -> None:
    key = bytes(range(16))
    plaintext = fixture_bytes("song_a.flac")
    encrypted = encrypt_ctr(plaintext, key)
    assert decrypt_bytes(encrypted, key.hex()) == plaintext
    assert decrypt_bytes(encrypted, key.hex().upper()) == plaintext


def test_streaming_decryptor_matches_whole_buffer() -> None:
    key = bytes(range(16))
    plaintext = b"streamed encraw payload" * 97
    encrypted = encrypt_ctr(plaintext, key)
    decryptor = AesCtrDecryptor(key.hex())
    output = b"".join(
        decryptor.update(encrypted[offset : offset + 13])
        for offset in range(0, len(encrypted), 13)
    )
    assert output + decryptor.finalize() == plaintext


def test_streaming_decryptor_resumes_at_arbitrary_offset() -> None:
    key = bytes(range(16))
    plaintext = fixture_bytes("song_a.flac")
    encrypted = encrypt_ctr(plaintext, key)
    for offset in (0, 1, 15, 16, 20003):
        decryptor = AesCtrDecryptor(key.hex(), offset=offset)
        assert decryptor.update(encrypted[offset:]) + decryptor.finalize() == plaintext[offset:]


def test_invalid_key_is_rejected_without_echoing_it() -> None:
    secret = "deadbeef"
    with pytest.raises(DecryptError) as error:
        decrypt_bytes(b"data", secret)
    assert secret not in str(error.value)
    assert validate_key(secret) is False
    assert validate_key(None) is False
    assert validate_key(bytes(16)) is True


# ------------------------------------------------------------------- quality


def test_codec_mapping_is_factual() -> None:
    assert classify_codec("flac").extension == ".flac"
    assert classify_codec("flac-mp4").format == "m4a"
    assert classify_codec("flac-mp4").extension == ".m4a"
    assert classify_codec("aac-mp4").extension == ".m4a"
    assert classify_codec("he-aac-mp4").extension == ".m4a"
    assert classify_codec("mp3").extension == ".mp3"
    assert classify_codec("opus").extension == ".opus"
    assert classify_codec("flac-mp4").container == "mp4"
    assert classify_codec("flac").container == "flac"


def test_select_source_falls_back_honestly() -> None:
    aac = SourceFile(url="https://cdn/a", codec="aac-mp4", quality="hq", transport="raw")
    mp3 = SourceFile(url="https://cdn/m", codec="mp3", quality="nq", transport="raw")
    selection = select_source_file([mp3, aac], DEFAULT_QUALITY_ORDER)
    assert selection.source is aac
    assert selection.actual_quality == "aac"
    assert selection.requested_quality == "flac"
    assert selection.fell_back is True
    assert selection.codec.extension == ".m4a"

    only_mp3 = select_source_file([mp3], DEFAULT_QUALITY_ORDER)
    assert only_mp3.actual_quality == "mp3"
    assert only_mp3.fell_back is True


def test_select_source_prefers_flac_and_full_track_over_preview() -> None:
    flac = SourceFile(url="https://cdn/f", codec="flac", quality="lossless", transport="raw")
    aac = SourceFile(url="https://cdn/a", codec="aac-mp4", quality="hq", transport="raw")
    preview = SourceFile(
        url="https://cdn/p",
        codec="flac",
        quality="lossless",
        transport="raw",
        preview=True,
    )
    assert select_source_file([aac, flac]).source is flac

    full_mp3 = SourceFile(url="https://cdn/m", codec="mp3", quality="nq", transport="raw")
    selection = select_source_file([preview, full_mp3])
    assert selection.source is full_mp3
    assert selection.preview is False


def test_describe_source_keeps_transport_and_preview_factual() -> None:
    source = SourceFile(
        url="https://cdn/x",
        codec="aac-mp4",
        quality="hq",
        transport="encraw",
        key="00" * 16,
        preview=True,
    )
    selection = describe_source(source)
    assert selection.transport == "encraw"
    assert selection.preview is True
    assert selection.actual_quality == "aac"


def test_quality_order_is_normalised() -> None:
    assert normalize_quality_order(["mp3", "flac", "mp3", "bogus"]) == ("mp3", "flac")
    assert normalize_quality_order([]) == DEFAULT_QUALITY_ORDER


# ----------------------------------------------------------------- integrity


def test_integrity_hashes_and_sniffs_containers(tmp_path: Path) -> None:
    flac = tmp_path / "a.flac"
    flac.write_bytes(fixture_bytes("song_a.flac"))
    report = verify_file(flac, expected_container="flac")
    assert report.sha256 == hashlib.sha256(flac.read_bytes()).hexdigest()
    assert report.size == flac.stat().st_size
    assert sniff_bytes(b"fLaC\x00\x00") == "flac"
    assert sniff_bytes(b"\x00\x00\x00\x18ftypisom") == "mp4"
    assert sniff_bytes(b"ID3\x04\x00") == "mpeg"
    assert sniff_bytes(b"RIFF\x00\x00\x00\x00WAVE") == "wav"
    assert sniff_bytes(b"OggS\x00\x02") == "ogg"
    assert sniff_bytes(b"<html>") == "unknown"


def test_integrity_rejects_size_and_container_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(fixture_bytes("song_a.flac"))
    with pytest.raises(IntegrityError):
        verify_file(path, expected_size=1)
    with pytest.raises(IntegrityError):
        verify_file(path, expected_container="mp4")
    with pytest.raises(IntegrityError):
        sha256_file(tmp_path / "missing.flac")


# ---------------------------------------------------------------------- tags


@pytest.mark.parametrize(
    ("fixture", "container"),
    [
        ("song_a.mp3", "id3"),
        ("song_a.flac", "vorbis"),
        ("song_c.m4a", "mp4"),
    ],
)
def test_write_tags_for_each_container(
    tmp_path: Path, fixture: str, container: str
) -> None:
    path = tmp_path / fixture
    shutil.copy2(FIXTURES / fixture, path)
    cover = fixture_bytes("cover.jpg")

    report = write_tags(path, TAGS, cover=cover)
    assert report.written is True
    assert report.container == container
    assert report.warning is None

    audio = mutagen.File(path)
    assert audio is not None
    if container == "id3":
        tags = audio.tags
        assert tags["TIT2"].text == ["Ночной эфир"]
        assert tags["TPE1"].text == ["Тестовый Оркестр", "Второй Артист"]
        assert tags["TPE2"].text == ["Тестовый Оркестр"]
        assert tags["TALB"].text == ["Mock Album"]
        assert str(tags["TDRC"].text[0]) == "2024"
        assert tags["TCON"].text == ["Electronic"]
        assert tags["TRCK"].text == ["1"]
        assert tags["TPOS"].text == ["1"]
        assert tags["TPUB"].text == ["Mock Label"]
        assert tags.getall("APIC")[0].data == cover
    elif container == "vorbis":
        assert audio["title"] == ["Ночной эфир"]
        assert audio["artist"] == ["Тестовый Оркестр", "Второй Артист"]
        assert audio["albumartist"] == ["Тестовый Оркестр"]
        assert audio["album"] == ["Mock Album"]
        assert audio["date"] == ["2024"]
        assert audio["genre"] == ["Electronic"]
        assert audio["tracknumber"] == ["1"]
        assert audio["discnumber"] == ["1"]
        assert audio["label"] == ["Mock Label"]
        assert audio.pictures[0].data == cover
    else:
        tags = audio.tags
        assert tags["\xa9nam"] == ["Ночной эфир"]
        assert tags["\xa9ART"] == ["Тестовый Оркестр", "Второй Артист"]
        assert tags["aART"] == ["Тестовый Оркестр"]
        assert tags["\xa9alb"] == ["Mock Album"]
        assert tags["\xa9day"] == ["2024"]
        assert tags["\xa9gen"] == ["Electronic"]
        assert tags["trkn"] == [(1, 0)]
        assert tags["disk"] == [(1, 0)]
        assert bytes(tags["----:com.apple.iTunes:LABEL"][0]) == b"Mock Label"
        assert bytes(tags["covr"][0]) == cover


def test_tag_writer_warns_instead_of_failing_on_flac_in_mp4(tmp_path: Path) -> None:
    # Mutagen cannot read this fake FLAC-in-MP4 container; the download must
    # still succeed with the factual m4a format and a warning.
    path = tmp_path / "flac_in_mp4.m4a"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 128)
    report = write_tags(path, TAGS)
    assert report.written is False
    assert report.container == "mp4"
    assert report.warning


def test_tag_writer_warns_for_unsupported_suffix(tmp_path: Path) -> None:
    path = tmp_path / "raw.aac"
    path.write_bytes(b"\xff\xf1" + b"\x00" * 32)
    report = write_tags(path, TAGS)
    assert report.written is False
    assert report.warning is not None


# ------------------------------------------------------------------ pipeline


@pytest.mark.asyncio
async def test_pipeline_downloads_direct_and_writes_expected_layout(
    settings: Settings,
) -> None:
    body = fixture_bytes("song_a.mp3")
    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(url="https://cdn/audio", codec="mp3", quality="mp3", transport="raw")
    )
    pipeline = make_pipeline(settings, source, session, progress_interval_s=3600)
    progress = Progress()

    result = await pipeline.run(make_task(), progress=progress)

    expected = (
        settings.paths.downloads
        / "Тестовый Оркестр"
        / "Mock Album (2024)"
        / "Disc 1"
        / "01 - Ночной эфир.mp3"
    )
    assert Path(result.path) == expected.resolve()
    assert result.format == "mp3"
    assert result.quality == "mp3"
    assert result.title == TAGS.title
    assert result.artists == TAGS.artists
    assert result.checksum == sha256_file(expected)
    assert result.bytes_written == expected.stat().st_size
    assert not (settings.paths.downloads / ".7.part").exists()
    assert progress.events[0] == (0, len(body))
    assert progress.events[-1] == (result.bytes_written, result.bytes_written)
    # Throttled: initial, wire-complete and (if tags changed the size) final.
    assert len(progress.events) <= 3

    audio = mutagen.File(expected)
    assert audio is not None
    assert audio.tags["TIT2"].text == ["Ночной эфир"]


@pytest.mark.asyncio
async def test_pipeline_reports_factual_aac_fallback_as_m4a(settings: Settings) -> None:
    body = fixture_bytes("song_c.m4a")
    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(
            url="https://cdn/aac",
            codec="aac-mp4",
            quality="aac",
            transport="raw",
        )
    )
    pipeline = make_pipeline(settings, source, session)

    result = await pipeline.run(make_task(task_id=8), progress=Progress())

    final = Path(result.path)
    assert result.format == "m4a"
    assert result.quality == "aac"
    assert final.suffix == ".m4a"
    assert sniff_bytes(final.read_bytes()[:16]) == "mp4"
    assert result.checksum == sha256_file(final)
    audio = mutagen.File(final)
    assert audio is not None
    assert audio.tags["\xa9nam"] == ["Ночной эфир"]


@pytest.mark.asyncio
async def test_pipeline_encraw_streams_and_decrypts(settings: Settings) -> None:
    plaintext = fixture_bytes("song_a.flac")
    key = bytes(range(16))
    encrypted = encrypt_ctr(plaintext, key)
    session = FakeSession(serve_body(encrypted))
    source = FakeSource(
        SourceFile(
            url="https://cdn/encraw",
            codec="flac",
            quality="lossless",
            transport="encraw",
            key=key.hex(),
        )
    )
    pipeline = make_pipeline(settings, source, session)

    result = await pipeline.run(make_task(), progress=Progress())

    final = Path(result.path)
    assert final.read_bytes()[:4] == b"fLaC"
    assert result.format == "flac"
    assert result.quality == "flac"
    assert result.checksum == sha256_file(final)
    assert sniff_bytes(final.read_bytes()[:16]) == "flac"
    assert not (settings.paths.downloads / ".7.part").exists()


@pytest.mark.asyncio
async def test_pipeline_encraw_resumes_keystream_from_part_offset(
    settings: Settings,
) -> None:
    plaintext = fixture_bytes("song_a.flac")
    key = bytes(range(16))
    encrypted = encrypt_ctr(plaintext, key)
    start = 20003  # deliberately not a 16-byte boundary
    settings.paths.downloads.mkdir(parents=True)
    part = settings.paths.downloads / ".44.part"
    part.write_bytes(plaintext[:start])

    session = FakeSession(serve_body(encrypted))
    source = FakeSource(
        SourceFile(
            url="https://cdn/encraw",
            codec="opus",
            quality="opus",
            transport="encraw",
            key=key.hex(),
        )
    )
    pipeline = make_pipeline(settings, source, session, metadata=None)
    progress = Progress()

    result = await pipeline.run(make_task(task_id=44), progress=progress)

    assert session.requests[0][1]["headers"]["Range"] == f"bytes={start}-"
    assert progress.events[0] == (start, len(plaintext))
    final = Path(result.path)
    # Unknown codec -> no tag rewrite, so the file is exactly the decrypted bytes.
    assert final.read_bytes() == plaintext
    assert result.format == "opus"
    assert not part.exists()


@pytest.mark.asyncio
async def test_pipeline_resumes_from_part_with_range(settings: Settings) -> None:
    body = fixture_bytes("song_a.flac")
    start = 20000
    settings.paths.downloads.mkdir(parents=True)
    part = settings.paths.downloads / ".42.part"
    part.write_bytes(body[:start])

    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(url="https://cdn/flac", codec="flac", quality="lossless", transport="raw")
    )
    pipeline = make_pipeline(settings, source, session)
    progress = Progress()

    result = await pipeline.run(make_task(task_id=42), progress=progress)

    assert session.requests[0][1]["headers"]["Range"] == f"bytes={start}-"
    assert progress.events[0] == (start, len(body))
    assert progress.events[-1] == (result.bytes_written, result.bytes_written)
    assert not part.exists()
    final = Path(result.path)
    assert final.read_bytes()[:4] == b"fLaC"
    assert result.checksum == sha256_file(final)


@pytest.mark.asyncio
async def test_pipeline_restarts_when_server_ignores_range(settings: Settings) -> None:
    body = fixture_bytes("song_a.mp3")
    settings.paths.downloads.mkdir(parents=True)
    part = settings.paths.downloads / ".43.part"
    part.write_bytes(b"stale partial data")

    session = FakeSession(serve_body(body, support_range=False))
    source = FakeSource(
        SourceFile(url="https://cdn/mp3", codec="mp3", quality="mp3", transport="raw")
    )
    pipeline = make_pipeline(settings, source, session)

    result = await pipeline.run(make_task(task_id=43), progress=Progress())

    final = Path(result.path)
    assert result.checksum == sha256_file(final)
    # The stale prefix must not survive; tags are written at the end, so the
    # audio itself starts with an ID3 tag and remains a valid MP3.
    assert sniff_bytes(final.read_bytes()[:16]) in {"mpeg", "unknown"}
    assert not part.exists()


@pytest.mark.asyncio
async def test_pipeline_rejects_traversal_template(settings: Settings) -> None:
    settings.downloads.path_template = "../{title}{ext}"
    body = fixture_bytes("song_a.mp3")
    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(url="https://cdn/mp3", codec="mp3", quality="mp3", transport="raw")
    )
    pipeline = make_pipeline(settings, source, session)

    with pytest.raises(PathTemplateError):
        await pipeline.run(make_task(), progress=Progress())
    assert session.requests == []
    assert not (settings.paths.downloads.parent / "Ночной эфир.mp3").exists()


@pytest.mark.asyncio
async def test_pipeline_sanitises_slashes_in_title(settings: Settings) -> None:
    body = fixture_bytes("song_a.mp3")
    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(url="https://cdn/mp3", codec="mp3", quality="mp3", transport="raw")
    )
    metadata = TrackTags(title="AC/DC - Live", artists=("AC/DC",))
    pipeline = make_pipeline(settings, source, session, metadata=metadata)

    result = await pipeline.run(make_task(task_id=9), progress=Progress())

    final = Path(result.path)
    assert final.name == "00 - AC_DC - Live.mp3"
    assert final.parent == (
        settings.paths.downloads / "AC_DC" / "Unknown Album" / "Disc 1"
    ).resolve()
    assert final.is_relative_to(settings.paths.downloads.resolve())


@pytest.mark.asyncio
async def test_pipeline_cleans_part_on_success_and_non_retryable_error(
    settings: Settings,
) -> None:
    body = fixture_bytes("song_a.mp3")
    source = FakeSource(
        SourceFile(url="https://cdn/mp3", codec="mp3", quality="mp3", transport="raw")
    )
    ok = make_pipeline(settings, source, FakeSession(serve_body(body)))
    await ok.run(make_task(task_id=11), progress=Progress())
    assert not (settings.paths.downloads / ".11.part").exists()

    failing = make_pipeline(
        settings, source, FakeSession(lambda url, kwargs: FakeResponse(status=404))
    )
    with pytest.raises(DownloadHttpError) as error:
        await failing.run(make_task(task_id=12), progress=Progress())
    assert error.value.status == 404
    assert not (settings.paths.downloads / ".12.part").exists()

    leftover = settings.paths.downloads / ".12.part"
    leftover.write_bytes(b"x")
    assert await ok.discard_partial(12) is True
    assert not leftover.exists()
    assert await ok.discard_partial(12) is False


@pytest.mark.asyncio
async def test_pipeline_keeps_part_on_retryable_http_error(settings: Settings) -> None:
    settings.paths.downloads.mkdir(parents=True)
    part = settings.paths.downloads / ".13.part"
    part.write_bytes(b"resumable")
    source = FakeSource(
        SourceFile(url="https://cdn/mp3", codec="mp3", quality="mp3", transport="raw")
    )
    pipeline = make_pipeline(
        settings, source, FakeSession(lambda url, kwargs: FakeResponse(status=503))
    )

    with pytest.raises(DownloadHttpError) as error:
        await pipeline.run(make_task(task_id=13), progress=Progress())
    assert error.value.status == 503
    assert part.read_bytes() == b"resumable"


@pytest.mark.asyncio
async def test_pipeline_embeds_and_writes_1000px_cover(settings: Settings) -> None:
    body = fixture_bytes("song_a.mp3")
    cover_body = fixture_bytes("cover.jpg")

    def handler(url: str, kwargs: dict[str, Any]) -> FakeResponse:
        if "avatars.yandex.net" in url:
            return FakeResponse(cover_body, headers={"Content-Length": str(len(cover_body))})
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    source = FakeSource(
        SourceFile(
            url="https://cdn/audio",
            codec="mp3",
            quality="mp3",
            transport="raw",
            cover_url="https://avatars.yandex.net/get-cover/1000x1000",
        )
    )
    pipeline = make_pipeline(settings, source, FakeSession(handler))

    result = await pipeline.run(make_task(task_id=14), progress=Progress())

    final = Path(result.path)
    cover_file = final.parent / "cover.jpg"
    assert cover_file.is_file()
    from PIL import Image

    with Image.open(cover_file) as image:
        assert image.size == (1000, 1000)
    audio = mutagen.File(final)
    assert audio is not None
    assert audio.tags.getall("APIC")[0].data == cover_file.read_bytes()


@pytest.mark.asyncio
async def test_pipeline_skips_cover_host_outside_allowlist(settings: Settings) -> None:
    body = fixture_bytes("song_a.mp3")
    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(
            url="https://cdn/audio",
            codec="mp3",
            quality="mp3",
            transport="raw",
            cover_url="https://evil.example/cover.jpg",
        )
    )
    pipeline = make_pipeline(settings, source, session)

    result = await pipeline.run(make_task(task_id=15), progress=Progress())

    assert len(session.requests) == 1
    assert not (Path(result.path).parent / "cover.jpg").exists()


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_task_title_without_metadata(
    settings: Settings,
) -> None:
    body = fixture_bytes("song_a.mp3")
    session = FakeSession(serve_body(body))
    source = FakeSource(
        SourceFile(url="https://cdn/mp3", codec="mp3", quality="mp3", transport="raw")
    )

    pipeline = DownloadPipeline(settings, source, session=session)
    result = await pipeline.run(make_task(), progress=Progress())

    assert result.title == "Track"
    assert result.artists == ("Unknown Artist",)
    assert Path(result.path).parent.parent.name == "Unknown Album"
