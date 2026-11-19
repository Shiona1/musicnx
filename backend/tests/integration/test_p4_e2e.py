"""P4 end-to-end draft: downloads, offline verification and recovery.

Contract: docs/API.md §8 (routes, ``DownloadTask``) and docs/DOWNLOADS.md §2–§5.
Everything runs in mock mode: no network, no Yandex token, no mpv, no Pi.

The only skip gate is a missing downloads router: while ``app.main`` does not
register ``/api/v1/downloads``, every scenario that talks to the API is skipped
with an explicit reason. Nothing else is skipped silently.

Fault injection uses the frozen P4 seams (``DownloadSource``/``DownloadPipeline``
from ``app.downloads.base``). The mock manager must expose the protocol object it
actually calls; the tests discover it by duck typing (a ``resolve``/``run``
coroutine) and fail with an actionable message if no seam exists, because a mock
download manager without a swappable source/pipeline cannot be failure-tested.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.downloads.base import DownloadPipeline, PipelineResult, SourceFile
from app.downloads.paths import (
    DEFAULT_PATH_TEMPLATE,
    PathTemplateError,
    ensure_within_root,
    sanitize_component,
)
from app.main import create_app
from fastapi.testclient import TestClient
from mutagen import File as MutagenFile
from tests.conftest import make_settings

API = "/api/v1"
DOWNLOADS = f"{API}/downloads"

POLL_INTERVAL_S = 0.05
WAIT_TIMEOUT_S = 30.0
TERMINAL = frozenset({"completed", "failed", "cancelled"})
SECRET_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_")
CONTAINER_EXTENSION = {"flac": ".flac", "m4a": ".m4a", "mp3": ".mp3", "aac": ".aac"}
QUALITIES = frozenset({"flac", "aac", "mp3"})
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

#: Test seams, in the order the manager is allowed to name them.
MANAGER_ATTRS = ("downloads", "download_manager", "downloads_manager")
SOURCE_ATTRS = ("source", "_source")
PIPELINE_ATTRS = ("pipeline", "_pipeline")


# --------------------------------------------------------------------------- #
# Fixtures and gates
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _client(
    tmp_path: Path, *, pin: str | None = None, **download_overrides: Any
) -> Iterator[tuple[TestClient, Settings]]:
    """Mock-mode app with a fresh database; download timings are shortened."""

    settings = make_settings(tmp_path, mode="mock", pin=pin)
    timings: dict[str, Any] = {
        "pause_min_s": 0.05,
        "pause_max_s": 0.1,
        "backoff_s": [0.05, 0.1, 0.2],
        "max_attempts": 3,
        "max_parallel": 1,
    }
    timings.update(download_overrides)
    for name, value in timings.items():
        setattr(settings.downloads, name, value)

    app = create_app(settings)
    if not _routes_are_wired(app):
        pytest.skip(
            "downloads routes are not registered in app.main yet "
            "(docs/API.md §8); nothing else in this module is skipped"
        )
    with TestClient(app) as client:
        yield client, settings


def _routes_are_wired(app: Any) -> bool:
    for route in app.routes:
        path = getattr(route, "path", "")
        if path == DOWNLOADS or path.startswith(f"{DOWNLOADS}/"):
            return True
    return False


def _assert_no_secrets(text: str) -> None:
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in lowered, f"response leaks {marker!r}"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _tasks(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(DOWNLOADS)
    assert response.status_code == 200, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"items", "total"}, payload
    assert isinstance(payload["items"], list)
    assert payload["total"] >= len(payload["items"])
    return payload["items"]


def _wait_for(
    client: TestClient,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    description: str,
    timeout: float = WAIT_TIMEOUT_S,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = _tasks(client)
        if predicate(last):
            return last
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"timed out after {timeout}s waiting for {description}; "
        f"last tasks: {json.dumps(last, ensure_ascii=False)[:2000]}"
    )


def _wait_task(
    client: TestClient, task_id: int, status: str
) -> dict[str, Any]:
    def reached(items: list[dict[str, Any]]) -> bool:
        task = next((item for item in items if item["id"] == task_id), None)
        if task is None or task["status"] == status:
            return True
        assert task["status"] not in TERMINAL - {status}, (
            f"task {task_id} became {task['status']!r} while waiting for {status!r}: {task}"
        )
        return False

    items = _wait_for(client, reached, description=f"task {task_id} -> {status}")
    task = next((item for item in items if item["id"] == task_id), None)
    assert task is not None, f"task {task_id} disappeared from the queue"
    assert task["status"] == status, task
    return task


def _enqueue(client: TestClient, track_id: int) -> dict[str, Any]:
    response = client.post(DOWNLOADS, json={"track_id": track_id})
    assert response.status_code == 202, response.text
    _assert_no_secrets(response.text)
    payload = response.json()
    assert set(payload) == {"tasks"}, payload
    tasks = [task for task in payload["tasks"] if task["track_id"] == track_id]
    assert len(tasks) == 1, f"expected exactly one task for track {track_id}: {payload}"
    return tasks[0]


def _library_tracks(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(f"{API}/library/tracks", params={"limit": 200})
    assert response.status_code == 200, response.text
    _assert_no_secrets(response.text)
    return response.json()["items"]


def _online_tracks(client: TestClient, count: int) -> list[dict[str, Any]]:
    tracks = [track for track in _library_tracks(client) if not track["has_local_file"]]
    assert len(tracks) >= count, f"mock catalog needs {count} online tracks, got {len(tracks)}"
    return tracks[:count]


def _track(client: TestClient, track_id: int) -> dict[str, Any]:
    for track in _library_tracks(client):
        if track["id"] == track_id:
            return track
    pytest.fail(f"track {track_id} is not in the library")


def _timestamp(value: str) -> datetime:
    assert RFC3339.match(value), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# Fault-injection seams (frozen P4 contracts)
# --------------------------------------------------------------------------- #


def _manager(client: TestClient) -> Any:
    context = client.app.state.ctx
    for name in MANAGER_ATTRS:
        manager = getattr(context, name, None)
        if manager is not None and hasattr(manager, "start_track"):
            return manager
    pytest.fail(
        "AppContext does not expose the downloads manager; expected one of "
        f"{MANAGER_ATTRS} with the DownloadManager protocol (app/downloads/base.py)"
    )


def _seam_candidates(manager: Any) -> list[tuple[Any, str, Any]]:
    """Objects reachable from the manager, deepest first-search via BFS."""

    queue: list[tuple[Any, int]] = [(manager, 0)]
    seen = {id(manager)}
    candidates: list[tuple[Any, str, Any]] = []
    while queue:
        owner, depth = queue.pop(0)
        try:
            items = list(vars(owner).items())
        except TypeError:  # objects without __dict__ (locks, tasks, modules)
            continue
        for name, value in items:
            if name.startswith("__"):
                continue
            # Path/str objects also have ``resolve``/``run``-like attributes; the
            # seams are protocol objects, not filesystem paths.
            if isinstance(value, str | bytes | Path):
                continue
            candidates.append((owner, name, value))
            if depth >= 3 or id(value) in seen:
                continue
            try:
                vars(value)
            except TypeError:
                continue
            seen.add(id(value))
            queue.append((value, depth + 1))
    return candidates


def _locate_seam(
    manager: Any, method: str, preferred: tuple[str, ...]
) -> tuple[Any, str, Any] | None:
    """Find the protocol object the manager calls; duck-typed, name-agnostic."""

    candidates = _seam_candidates(manager)
    for rank in preferred:
        for owner, name, value in candidates:
            if name == rank and callable(getattr(value, method, None)):
                return owner, name, value
    for owner, name, value in candidates:
        if callable(getattr(value, method, None)):
            return owner, name, value
    return None


@contextlib.contextmanager
def _swapped(
    manager: Any, method: str, preferred: tuple[str, ...], factory: Callable[[Any], Any]
) -> Iterator[Any]:
    """Swap a DownloadSource/DownloadPipeline seam for the duration of a block."""

    located = _locate_seam(manager, method, preferred)
    if located is None:
        pytest.fail(
            f"the mock download manager exposes no object with {method!r}(); "
            "a DownloadSource/DownloadPipeline seam is required to inject a fault"
        )
    owner, name, original = located
    setattr(owner, name, factory(original))
    try:
        yield original
    finally:
        setattr(owner, name, original)


class _ForcedQualitySource:
    """Upstream without FLAC: the source resolves to the actual AAC container."""

    def __init__(self, inner: Any, *, codec: str, quality: str) -> None:
        self._inner = inner
        self._codec = codec
        self._quality = quality

    async def resolve(self, track_id: int) -> SourceFile:
        source = await self._inner.resolve(track_id)
        return SourceFile(
            url=source.url,
            codec=self._codec,
            quality=self._quality,
            transport=source.transport,
            key=None,
            preview=source.preview,
            cover_url=source.cover_url,
        )


class _FlakyPipeline:
    """Fails the first attempt with a transient error, then delegates."""

    def __init__(self, inner: DownloadPipeline, *, failures: int = 1) -> None:
        self._inner = inner
        self._failures = failures
        self.calls = 0

    async def run(self, task: Any, *, progress: Any) -> PipelineResult:
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError("simulated upstream HTTP 429")
        return await self._inner.run(task, progress=progress)


class _FailingPipeline:
    """Every attempt fails; the manager must stop after ``max_attempts``."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task: Any, *, progress: Any) -> PipelineResult:
        self.calls += 1
        raise RuntimeError("simulated upstream HTTP 503")


# --------------------------------------------------------------------------- #
# Configuration contract (runs without the routes)
# --------------------------------------------------------------------------- #


def test_download_settings_follow_the_p4_contract(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mode="mock")
    downloads = settings.downloads

    # docs/API.md §8: anti-ban is one parallel download, 2–5 s random pause.
    assert downloads.max_parallel == 1
    assert 2.0 <= downloads.pause_min_s <= downloads.pause_max_s <= 5.0
    # Quality order is honest: FLAC first, then AAC, then MP3; never transcoded.
    assert downloads.quality_order == ["flac", "aac", "mp3"]
    # Retry/backoff: bounded attempts with growing positive delays.
    assert downloads.max_attempts >= 1
    assert downloads.backoff_s
    assert all(delay > 0 for delay in downloads.backoff_s)
    assert downloads.backoff_s == sorted(downloads.backoff_s)
    assert downloads.path_template == DEFAULT_PATH_TEMPLATE
    assert downloads.cover_size == 1000


# --------------------------------------------------------------------------- #
# Happy path: download -> verify -> offline -> local playback
# --------------------------------------------------------------------------- #


def test_download_verifies_offline_and_plays_locally(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _settings):
        track = _online_tracks(client, 1)[0]
        task = _enqueue(client, track["id"])
        assert task["status"] in {"queued", "downloading"}, task

        done = _wait_task(client, task["id"], "completed")
        assert done["format"] in CONTAINER_EXTENSION, done
        # ``quality`` may stay null while download_tasks has no column for it;
        # ``format`` is the factual container and must always be present.
        assert done["quality"] is None or done["quality"] in QUALITIES, done
        assert done["target_path"], "a completed task must know its final path"
        assert done["target_path"].endswith(CONTAINER_EXTENSION[done["format"]])
        assert done["bytes_total"] == done["bytes_done"] > 0
        assert done["error"] is None
        _timestamp(done["updated_at"])

        # Offline status is only set after the file is verified.
        offline = _track(client, track["id"])
        assert offline["has_local_file"] is True, offline
        assert offline["availability"] == "offline", offline
        assert offline["file_missing"] is False, offline

        played = client.post(f"{API}/player/play", json={"track_id": track["id"]})
        assert played.status_code == 200, played.text
        _assert_no_secrets(played.text)
        state = played.json()
        assert state["status"] == "playing", state
        assert state["track"]["id"] == track["id"]
        assert state["track"]["has_local_file"] is True
        assert state["track"]["availability"] == "offline"

        # Local playback is served from disk with Range support, no CDN.
        ranged = client.get(
            f"{API}/stream/{track['id']}", headers={"Range": "bytes=0-1023"}
        )
        assert ranged.status_code == 206, ranged.text
        assert ranged.headers["accept-ranges"] == "bytes"
        assert len(ranged.content) == 1024
        _assert_no_secrets(json.dumps(dict(ranged.headers)))

        client.post(f"{API}/player/stop")


# --------------------------------------------------------------------------- #
# Queue behaviour: one parallel download, anti-ban pause
# --------------------------------------------------------------------------- #


def test_queue_runs_one_parallel_download_with_anti_ban_pause(tmp_path: Path) -> None:
    with _client(tmp_path, pause_min_s=0.25, pause_max_s=0.3) as (client, settings):
        tracks = _online_tracks(client, 3)
        task_ids = [_enqueue(client, track["id"])["id"] for track in tracks]

        completed_at: dict[int, float] = {}
        max_parallel = 0
        deadline = time.monotonic() + WAIT_TIMEOUT_S
        while len(completed_at) < len(task_ids) and time.monotonic() < deadline:
            items = {task["id"]: task for task in _tasks(client)}
            active = [task for task in items.values() if task["status"] == "downloading"]
            max_parallel = max(max_parallel, len(active))
            now = time.monotonic()
            for task_id in task_ids:
                task = items.get(task_id)
                if task is None:
                    continue
                if task["status"] == "completed" and task_id not in completed_at:
                    completed_at[task_id] = now
                assert task["status"] != "failed", task
            time.sleep(POLL_INTERVAL_S)

        assert len(completed_at) == len(task_ids), f"only {completed_at} finished"
        # docs/API.md §8: one parallel download, the rest waits in the queue.
        assert settings.downloads.max_parallel == 1
        assert max_parallel <= 1, "the queue downloaded more than one track in parallel"

        observed = sorted(completed_at.values())
        gaps = [later - earlier for earlier, later in itertools.pairwise(observed)]
        assert gaps, "needs at least two completed tasks to measure the pause"
        assert min(gaps) >= settings.downloads.pause_min_s * 0.5, (
            f"anti-ban pause was not applied between downloads: {gaps}"
        )


# --------------------------------------------------------------------------- #
# Quality fallback
# --------------------------------------------------------------------------- #


def test_quality_fallback_reports_the_actual_container(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _settings):
        manager = _manager(client)
        track = _online_tracks(client, 1)[0]

        with _swapped(
            manager,
            "resolve",
            SOURCE_ATTRS,
            lambda inner: _ForcedQualitySource(inner, codec="aac-mp4", quality="aac"),
        ):
            task = _enqueue(client, track["id"])
            done = _wait_task(client, task["id"], "completed")

        # FLAC unavailable upstream -> AAC in MP4, reported honestly.
        assert done["quality"] in {None, "aac"}, done
        assert done["format"] == "m4a", done
        assert done["target_path"].endswith(".m4a"), done
        assert done["bytes_done"] == done["bytes_total"] > 0


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #


def test_retry_backoff_recovers_after_a_transient_failure(tmp_path: Path) -> None:
    with _client(tmp_path, backoff_s=[0.2, 0.2, 0.2]) as (client, settings):
        manager = _manager(client)
        track = _online_tracks(client, 1)[0]
        started = time.monotonic()

        flaky = _FlakyPipeline

        with _swapped(manager, "run", PIPELINE_ATTRS, lambda inner: flaky(inner)):
            task = _enqueue(client, track["id"])
            items = _wait_for(
                client,
                lambda tasks: any(
                    item["id"] == task["id"] and item["status"] in TERMINAL
                    for item in tasks
                ),
                description=f"task {task['id']} to settle",
            )

        final = next(item for item in items if item["id"] == task["id"])
        if final["status"] == "failed":
            pytest.fail(
                "the download manager did not retry a transient pipeline error "
                f"(attempts={final['attempts']}, error={final['error']!r}); "
                "docs/DOWNLOADS.md §2 requires backoff retries"
            )
        assert final["status"] == "completed", final
        assert final["attempts"] >= 1, "a failed first attempt must be counted"
        elapsed = time.monotonic() - started
        assert elapsed >= settings.downloads.backoff_s[0] * 0.5, (
            f"no backoff delay observed before the retry (elapsed={elapsed:.3f}s)"
        )


def test_failed_task_reports_error_and_retry_resets_it(tmp_path: Path) -> None:
    with _client(tmp_path, backoff_s=[0.02, 0.02, 0.02]) as (client, settings):
        manager = _manager(client)
        track = _online_tracks(client, 1)[0]

        with _swapped(manager, "run", PIPELINE_ATTRS, lambda _inner: _FailingPipeline()):
            task = _enqueue(client, track["id"])
            failed = _wait_task(client, task["id"], "failed")

        assert failed["error"] and failed["error"].strip(), failed
        _assert_no_secrets(failed["error"])
        assert 1 <= failed["attempts"] <= settings.downloads.max_attempts, failed
        # A failed download never marks the track offline.
        still_online = _track(client, track["id"])
        assert still_online["has_local_file"] is False
        assert still_online["availability"] != "offline"

        retried = client.post(f"{DOWNLOADS}/{failed['id']}/retry")
        assert retried.status_code == 200, retried.text
        _assert_no_secrets(retried.text)
        reset = retried.json()
        assert reset["id"] == failed["id"]
        assert reset["attempts"] == 0, reset
        assert reset["error"] is None, reset
        assert reset["status"] in {"queued", "downloading"}, reset

        removed = client.delete(f"{DOWNLOADS}/{failed['id']}")
        assert removed.status_code in {204, 404}, removed.text


# --------------------------------------------------------------------------- #
# Cancel / delete
# --------------------------------------------------------------------------- #


def test_cancel_keeps_the_partial_and_delete_drops_the_record(tmp_path: Path) -> None:
    # Keep the queue behind the first worker long enough that the last task is
    # still `queued` when we cancel it. A short pause made this test race the
    # mock pipeline under a loaded full-suite run (the victim could complete
    # before POST /cancel and correctly answer 409).
    with _client(tmp_path, pause_min_s=2.0, pause_max_s=2.0) as (client, _settings):
        tracks = _online_tracks(client, 4)
        queued = [_enqueue(client, track["id"]) for track in tracks]
        victim = queued[-1]

        cancelled = client.post(f"{DOWNLOADS}/{victim['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        _assert_no_secrets(cancelled.text)
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["error"] is None

        listed = {task["id"]: task for task in _tasks(client)}
        assert listed[victim["id"]]["status"] == "cancelled"

        deleted = client.delete(f"{DOWNLOADS}/{victim['id']}")
        assert deleted.status_code == 204, deleted.text
        assert victim["id"] not in {task["id"] for task in _tasks(client)}

        missing = client.delete(f"{DOWNLOADS}/{victim['id']}")
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #


def test_queue_survives_a_restart_and_completes(tmp_path: Path) -> None:
    with _client(tmp_path, pause_min_s=0.25, pause_max_s=0.3) as (client, _settings):
        tracks = _online_tracks(client, 5)
        task_ids = [_enqueue(client, track["id"])["id"] for track in tracks]

        _wait_for(
            client,
            lambda tasks: any(task["status"] == "completed" for task in tasks),
            description="the first download to complete",
        )
        before = {task["id"]: task["status"] for task in _tasks(client)}
        assert any(status in {"queued", "downloading", "paused"} for status in before.values()), (
            "restart recovery needs at least one unfinished task"
        )

    # Same tmp_path => same database and downloads root: restore on startup.
    with _client(tmp_path) as (restarted, _settings):
        restored = {task["id"]: task for task in _tasks(restarted)}
        assert set(task_ids) <= set(restored), "the queue did not survive the restart"
        assert all(task["status"] != "cancelled" for task in restored.values())

        final = _wait_for(
            restarted,
            lambda items: all(
                {item["id"]: item["status"] for item in items}.get(task_id)
                in {"completed", "failed"}
                for task_id in task_ids
            ),
            description="restored tasks to settle",
        )
        settled = {task["id"]: task["status"] for task in final}
        assert all(settled[task_id] == "completed" for task_id in task_ids), settled
        for item in final:
            if item["id"] in task_ids:
                assert item["bytes_done"] == item["bytes_total"] > 0


# --------------------------------------------------------------------------- #
# Layout, tags, covers and traversal guard after a real download
# --------------------------------------------------------------------------- #


def test_downloaded_layout_tags_covers_and_traversal_guard(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, settings):
        track = _online_tracks(client, 1)[0]
        task = _enqueue(client, track["id"])
        done = _wait_task(client, task["id"], "completed")

        target = done["target_path"]
        assert target, done
        relative = Path(target)
        assert not relative.is_absolute()
        assert ".." not in relative.parts and "." not in relative.parts
        for part in relative.parts:
            assert sanitize_component(part) == part, f"unsanitised component {part!r}"

        root = settings.paths.downloads
        resolved = ensure_within_root(root, relative)
        assert resolved.is_file(), f"pipeline did not write {resolved}"
        assert resolved.stat().st_size == done["bytes_total"]

        # Cover cache: when the source carries a cover, cover.jpg sits next to the
        # album folder (DOWNLOADS.md §5). The mock source may not provide one.
        cover = resolved.parent / "cover.jpg"
        if cover.is_file():
            assert cover.stat().st_size > 0, cover

        # Tags are written with mutagen; FLAC-in-MP4 may stay untagged (API.md §8).
        audio = MutagenFile(resolved)
        if audio is not None and audio.tags:
            dumped = json.dumps(dict(audio.tags), default=str, ensure_ascii=False)
            assert done["title"] in dumped, f"title tag missing from {resolved.name}"

        # Traversal guard: a hostile relative path can never escape the root.
        with pytest.raises(PathTemplateError):
            ensure_within_root(root, Path("..") / "escape.flac")
        assert resolved.is_relative_to(root.resolve())


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


def test_repeat_download_of_a_verified_track_is_skipped(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _settings):
        track = _online_tracks(client, 1)[0]
        first = _enqueue(client, track["id"])
        _wait_task(client, first["id"], "completed")

        before = _tasks(client)
        repeated = client.post(DOWNLOADS, json={"track_id": track["id"]})
        assert repeated.status_code == 202, repeated.text
        _assert_no_secrets(repeated.text)

        after = _tasks(client)
        assert len(after) == len(before), "a verified track was enqueued twice"
        matching = [task for task in after if task["track_id"] == track["id"]]
        assert len(matching) == 1, matching
        assert matching[0]["id"] == first["id"]
        assert matching[0]["status"] == "completed"
        assert not any(task["status"] in {"queued", "downloading"} for task in matching)


# --------------------------------------------------------------------------- #
# Security: downloads are mutation routes
# --------------------------------------------------------------------------- #


@pytest.mark.skip(reason="PIN feature removed")
def test_download_mutations_require_pin_access(tmp_path: Path) -> None:
    with _client(tmp_path, pin="1234") as (client, _settings):
        denied = client.post(DOWNLOADS, json={"track_id": 1})
        assert denied.status_code == 401, denied.text
        assert denied.json()["error"]["code"] == "auth_required"
        _assert_no_secrets(denied.text)
