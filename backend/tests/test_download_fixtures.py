"""Contract tests for the P4 download fixtures.

The JSON files under ``tests/fixtures/yandex_download`` are the mock download
source/pipeline/queue canned answers. They mirror the frozen P4 contracts:
``SourceFile`` and ``PipelineResult`` from ``app.downloads.base``,
``DownloadTask``/``DownloadTaskPage`` from ``app.core.models`` and the reviewed
route/WS shapes in docs/API.md §8 and §7.

They are normalized fixtures, not raw ``yandex_music`` objects:

* ``transport=encraw`` carries ``key_present=true`` and never the AES key value;
* audio URLs are opaque upstream paths without a signed query string;
* ``PipelineResult.path`` is stored relative to the downloads root: the runtime
  value is absolute (``verify_file`` resolves it), fixtures stay portable and
  mirror the persisted ``DownloadTask.target_path``;
* ``checksum`` values are deterministic sha256-shaped placeholders: the real
  smoke SHA-256 was observed on the Pi but is not stored in the repository
  (docs/P4_NOTES.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from app.core.models import DownloadTask, DownloadTaskPage
from app.downloads.base import PipelineResult, SourceFile
from app.downloads.paths import (
    DEFAULT_PATH_TEMPLATE,
    PathContext,
    ensure_within_root,
    render_relative_path,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "yandex_download"

SOURCE_FIXTURES = (
    "source_direct.json",
    "source_encraw.json",
    "source_encraw_flac_mp4.json",
    "source_fallback_aac.json",
    "source_preview.json",
)
PIPELINE_FIXTURES = ("pipeline_result.json", "pipeline_result_flac_mp4.json")
TASK_FIXTURE = "tasks.json"
PROGRESS_FIXTURE = "progress.json"
DONE_FIXTURE = "done.json"

EXPECTED_FIXTURES = frozenset(
    {*SOURCE_FIXTURES, *PIPELINE_FIXTURES, TASK_FIXTURE, PROGRESS_FIXTURE, DONE_FIXTURE}
)

#: Fixture keys that are explained by ``SourceFile`` (the rest is fixture context).
SOURCE_MODEL_KEYS = frozenset(
    {"url", "codec", "quality", "transport", "preview", "cover_url"}
)
SOURCE_CONTEXT_KEYS = frozenset({"track_id", "key_present"})

#: docs/YANDEX_API.md §3 codecs; FLAC-in-MP4 is a real container, not a codec swap.
CODECS = frozenset({"flac", "flac-mp4", "aac-mp4", "he-aac-mp4", "mp3"})
QUALITIES = frozenset({"flac", "aac", "mp3"})
TRANSPORTS = frozenset({"raw", "encraw"})
STATUSES = frozenset(
    {"queued", "downloading", "paused", "completed", "failed", "cancelled"}
)
#: Actual file container written for a codec (docs/API.md §8, YANDEX_API.md §3).
CODEC_CONTAINER = {
    "flac": "flac",
    "flac-mp4": "m4a",
    "aac-mp4": "m4a",
    "he-aac-mp4": "m4a",
    "mp3": "mp3",
}
CONTAINER_EXTENSION = {"flac": ".flac", "m4a": ".m4a", "mp3": ".mp3"}

#: docs/API.md §8: real smoke returned a 29,529,693-byte FLAC-in-MP4 file.
REAL_SMOKE_FLAC_MP4_BYTES = 29_529_693

SECRET_MARKERS = (
    "access_token",
    "refresh_token",
    "bearer ",
    "y0_",
    "key=",
    "token=",
    "signature",
    "sign=",
    "expires=",
)
FORBIDDEN_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "key",
        "signature",
        "sig",
        "secret",
        "cookie",
        "password",
    }
)
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict), f"{name} must be a JSON object"
    return payload


def fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


def _iter_keys(value: Any) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_keys(item)


def _iter_strings(value: Any) -> Any:
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, str):
        yield value


def _parse_timestamp(value: str) -> datetime:
    assert RFC3339.match(value), f"{value!r} is not RFC3339 with milliseconds"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _as_source_file(payload: dict[str, Any]) -> SourceFile:
    """Build the frozen dataclass; the fixture must not carry the AES key."""

    assert set(payload) == SOURCE_MODEL_KEYS | SOURCE_CONTEXT_KEYS, set(payload)
    assert isinstance(payload["key_present"], bool)
    return SourceFile(
        url=payload["url"],
        codec=payload["codec"],
        quality=payload["quality"],
        transport=payload["transport"],
        key=None,
        preview=payload["preview"],
        cover_url=payload["cover_url"],
    )


def _as_pipeline_result(payload: dict[str, Any]) -> PipelineResult:
    data = dict(payload)
    data["artists"] = tuple(payload["artists"])
    return PipelineResult(**data)


def _tasks() -> DownloadTaskPage:
    return DownloadTaskPage.model_validate(load_fixture(TASK_FIXTURE))


def _task_by_status(status: str) -> DownloadTask:
    matches = [task for task in _tasks().items if task.status == status]
    assert len(matches) == 1, f"tasks.json must carry exactly one {status!r} task"
    return matches[0]


def _rendered_path(result: PipelineResult) -> Path:
    context = PathContext(
        artists=", ".join(result.artists),
        album=result.album,
        year=result.year,
        disc=result.disc_number,
        track=result.track_number,
        title=result.title,
        ext=CONTAINER_EXTENSION[result.format],
    )
    return render_relative_path(DEFAULT_PATH_TEMPLATE, context)


def _assert_relative_target(path: str) -> None:
    pure = Path(path)
    assert not pure.is_absolute(), path
    assert not pure.drive, path
    assert "\\" not in path, "API paths use '/' separators"
    assert ".." not in pure.parts, path
    assert "." not in pure.parts, path
    for part in pure.parts:
        assert not ILLEGAL_PATH_CHARS.search(part), f"unsanitised component {part!r}"


# --------------------------------------------------------------------------- #
# Shape and secrecy
# --------------------------------------------------------------------------- #


def test_fixture_set_is_exactly_the_p4_contract() -> None:
    assert {path.name for path in fixture_paths()} == EXPECTED_FIXTURES


def test_no_secret_material_in_any_fixture() -> None:
    assert fixture_paths(), "no fixtures found"
    for path in fixture_paths():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in SECRET_MARKERS:
            assert marker not in lowered, f"{path.name} leaks {marker!r}"
        for key in _iter_keys(json.loads(text)):
            assert key.lower() not in FORBIDDEN_KEYS, (
                f"{path.name} contains forbidden field {key!r}; "
                "encraw fixtures carry key_present only"
            )


def test_audio_urls_are_not_signed_links() -> None:
    url_count = 0
    for path in fixture_paths():
        for value in _iter_strings(json.loads(path.read_text(encoding="utf-8"))):
            if not value.startswith("http"):
                continue
            url_count += 1
            parsed = urlparse(value)
            assert parsed.scheme == "https", value
            assert parsed.netloc, value
            assert parsed.query == "", f"{path.name} keeps a signed query: {value}"
            assert parsed.fragment == "", value
            assert parsed.username is None and parsed.password is None, value
            assert "token" not in value.lower(), value
            assert "sign" not in value.lower(), value
    assert url_count >= len(SOURCE_FIXTURES), "every source fixture carries one audio URL"


# --------------------------------------------------------------------------- #
# SourceFile fixtures (direct and encraw)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SOURCE_FIXTURES, ids=SOURCE_FIXTURES)
def test_source_fixture_matches_source_file_dataclass(name: str) -> None:
    payload = load_fixture(name)
    source = _as_source_file(payload)

    assert source.url == payload["url"]
    assert source.codec == payload["codec"]
    assert source.quality == payload["quality"]
    assert source.transport == payload["transport"]
    assert source.preview is payload["preview"]
    assert source.cover_url == payload["cover_url"]
    # The key value is never persisted in fixtures; only its presence is a fact.
    assert source.key is None
    assert not hasattr(source, "key_present")

    with pytest.raises(FrozenInstanceError):
        source.codec = "mp3"  # type: ignore[misc]


@pytest.mark.parametrize("name", SOURCE_FIXTURES, ids=SOURCE_FIXTURES)
def test_source_quality_codec_and_transport_are_documented_values(name: str) -> None:
    payload = load_fixture(name)
    assert payload["codec"] in CODECS
    assert payload["quality"] in QUALITIES
    assert payload["transport"] in TRANSPORTS
    assert isinstance(payload["preview"], bool)
    assert isinstance(payload["track_id"], int) and payload["track_id"] > 0
    assert isinstance(payload["key_present"], bool)
    if payload["transport"] == "encraw":
        assert payload["key_present"] is True, "encraw without key contradicts YANDEX_API §3"
    else:
        assert payload["key_present"] is False


def test_direct_and_encraw_sources_describe_the_same_file() -> None:
    direct = load_fixture("source_direct.json")
    encraw = load_fixture("source_encraw.json")

    assert direct["transport"] == "raw"
    assert encraw["transport"] == "encraw"
    assert direct["key_present"] is False
    assert encraw["key_present"] is True
    assert direct["url"] == encraw["url"], "same upstream file, different transport"

    stable_keys = set(direct) - {"transport", "key_present"}
    assert {key: direct[key] for key in stable_keys} == {
        key: encraw[key] for key in stable_keys
    }


def test_flac_mp4_source_records_the_real_smoke_container() -> None:
    payload = load_fixture("source_encraw_flac_mp4.json")
    source = _as_source_file(payload)

    assert source.codec == "flac-mp4"
    assert source.transport == "encraw"
    assert payload["key_present"] is True
    assert source.preview is False
    assert CODEC_CONTAINER[source.codec] == "m4a", (
        "FLAC-in-MP4 is stored as .m4a; the codec is not rewritten (docs/API.md §8)"
    )


def test_preview_source_is_the_only_preview() -> None:
    payloads = {name: load_fixture(name) for name in SOURCE_FIXTURES}
    previews = [(name, payload) for name, payload in payloads.items() if payload["preview"]]
    assert len(previews) == 1
    name, preview = previews[0]
    assert name == "source_preview.json"
    assert preview["preview"] is True
    assert preview["quality"] == "mp3"
    assert preview["transport"] == "raw"


# --------------------------------------------------------------------------- #
# DownloadTask / DownloadTaskPage fixtures
# --------------------------------------------------------------------------- #


def test_tasks_fixture_matches_the_pydantic_models() -> None:
    payload = load_fixture(TASK_FIXTURE)
    page = DownloadTaskPage.model_validate(payload)
    assert json.loads(page.model_dump_json()) == payload
    assert page.total == len(page.items) == 6


def test_tasks_cover_every_download_status() -> None:
    page = _tasks()
    assert {task.status for task in page.items} == STATUSES
    assert len({task.id for task in page.items}) == len(page.items)
    assert len({task.track_id for task in page.items}) == len(page.items)


def test_tasks_are_newest_first() -> None:
    created = [_parse_timestamp(task.created_at) for task in _tasks().items]
    assert created == sorted(created, reverse=True)


@pytest.mark.parametrize("status", sorted(STATUSES))
def test_task_fields_are_internally_consistent(status: str) -> None:
    task = _task_by_status(status)

    # ``created_at``/``updated_at`` are RFC3339 and monotonic per task.
    assert _parse_timestamp(task.updated_at) >= _parse_timestamp(task.created_at)

    if task.quality is not None:
        assert task.quality in QUALITIES
    if task.format is not None:
        assert task.format in CONTAINER_EXTENSION
    if task.target_path is not None:
        _assert_relative_target(task.target_path)

    if task.status == "completed":
        assert task.format is not None and task.target_path is not None
        assert task.bytes_total is not None
        assert task.bytes_done == task.bytes_total > 0
        assert task.error is None
        assert task.attempts >= 1
        assert task.target_path.endswith(CONTAINER_EXTENSION[task.format])
    elif task.status == "downloading":
        assert task.format is not None and task.target_path is not None
        assert task.bytes_total is not None
        assert 0 < task.bytes_done < task.bytes_total
        assert task.target_path.endswith(CONTAINER_EXTENSION[task.format])
    elif task.status == "paused":
        assert task.bytes_total is not None
        assert task.bytes_done < task.bytes_total
        assert task.error is None
    elif task.status == "failed":
        assert task.error and task.error.strip()
        assert task.attempts >= 1
        assert task.bytes_total is None or task.bytes_done < task.bytes_total
    elif task.status == "queued":
        assert task.bytes_done == 0
        assert task.attempts == 0
        assert task.target_path is None and task.format is None
        assert task.error is None
    elif task.status == "cancelled":
        assert task.error is None
        assert task.bytes_total is not None
        assert task.bytes_done < task.bytes_total


def test_completed_task_agrees_with_the_pipeline_result() -> None:
    task = _task_by_status("completed")
    result = _as_pipeline_result(load_fixture("pipeline_result.json"))

    assert task.title == result.title
    assert task.target_path == result.path
    assert task.quality == result.quality
    assert task.format == result.format
    assert task.bytes_done == task.bytes_total == result.bytes_written


# --------------------------------------------------------------------------- #
# PipelineResult fixtures (tags, layout, traversal guard)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", PIPELINE_FIXTURES, ids=PIPELINE_FIXTURES)
def test_pipeline_result_fixture_matches_the_dataclass(name: str) -> None:
    payload = load_fixture(name)
    result = _as_pipeline_result(payload)

    assert set(payload) == {
        "path",
        "format",
        "quality",
        "checksum",
        "bytes_written",
        "title",
        "artists",
        "album",
        "albumartist",
        "year",
        "genre",
        "track_number",
        "disc_number",
        "label",
    }
    assert result.format == payload["format"]
    assert result.quality == payload["quality"]
    assert result.artists == tuple(payload["artists"])
    assert result.artists, "PipelineResult.artists must record every artist"
    assert result.title.strip() and result.album
    assert result.year == 2024
    assert result.track_number == 1 and result.disc_number == 1
    assert SHA256_HEX.match(result.checksum), result.checksum
    assert result.bytes_written > 0


@pytest.mark.parametrize("name", PIPELINE_FIXTURES, ids=PIPELINE_FIXTURES)
def test_pipeline_result_path_follows_the_path_template(name: str, tmp_path: Path) -> None:
    result = _as_pipeline_result(load_fixture(name))

    assert Path(result.path) == _rendered_path(result)
    _assert_relative_target(result.path)
    assert result.path.endswith(CONTAINER_EXTENSION[result.format])

    root = tmp_path / "downloads"
    root.mkdir()
    resolved = ensure_within_root(root, Path(result.path))
    assert resolved.is_relative_to(root.resolve())
    assert resolved != root


def test_pipeline_results_keep_one_file_per_container() -> None:
    flac = _as_pipeline_result(load_fixture("pipeline_result.json"))
    flac_mp4 = _as_pipeline_result(load_fixture("pipeline_result_flac_mp4.json"))

    assert flac.quality == flac_mp4.quality == "flac"
    assert flac.format == "flac" and flac_mp4.format == "m4a"
    assert Path(flac.path).stem == Path(flac_mp4.path).stem
    assert Path(flac.path).parent == Path(flac_mp4.path).parent
    assert flac.checksum != flac_mp4.checksum, "different containers, different bytes"
    # docs/API.md §8: several qualities of one track are several media_files.
    assert flac.bytes_written != flac_mp4.bytes_written
    assert flac_mp4.bytes_written == REAL_SMOKE_FLAC_MP4_BYTES


# --------------------------------------------------------------------------- #
# WebSocket event fixtures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "event_type", "expected_status"),
    [
        (PROGRESS_FIXTURE, "download.progress", "downloading"),
        (DONE_FIXTURE, "download.done", "completed"),
    ],
)
def test_download_events_use_the_ws_envelope(
    name: str, event_type: str, expected_status: str
) -> None:
    payload = load_fixture(name)

    assert set(payload) == {"type", "ts", "payload"}
    assert payload["type"] == event_type
    _parse_timestamp(payload["ts"])

    event_payload = payload["payload"]
    assert set(event_payload) == {"task"}, "docs/API.md §7 wraps the task as {task}"
    task = DownloadTask.model_validate(event_payload["task"])
    assert task.status == expected_status
    assert task.updated_at == payload["ts"], "event ts is the task's last update"
    assert event_payload["task"] == _task_by_status(expected_status).model_dump(
        mode="json"
    )
