"""Contract tests for the P6 sync fixtures.

The JSON files under ``tests/fixtures/sync`` are canned ``sync_runs`` rows,
``GET /sync/status`` and ``GET /sync/history`` answers. They must mirror the
core DTOs (``SyncRun``/``SyncStatus``/``SyncHistory``) from docs/API.md §10 and
carry no token material. The history fixture must stay coherent with the status
fixture: the newest run is the same row in both.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from app.core.models import SyncHistory, SyncRun, SyncStatus
from pydantic import BaseModel

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "sync"

#: Fixtures validated directly by one core DTO.
MODEL_FIXTURES: dict[str, type[BaseModel]] = {
    "run_success.json": SyncRun,
    "run_repeat.json": SyncRun,
    "run_partial.json": SyncRun,
    "status.json": SyncStatus,
    "status_running.json": SyncStatus,
    "history.json": SyncHistory,
}

RUN_FIXTURES = ("run_success.json", "run_repeat.json", "run_partial.json")

EXPECTED_FIXTURES = frozenset(MODEL_FIXTURES)

SECRET_MARKERS = ("access_token", "refresh_token", "bearer ", "y0_")
FORBIDDEN_KEYS = {"token", "access_token", "refresh_token", "device_code", "authorization"}
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
TERMINAL = frozenset({"success", "partial", "failed", "cancelled"})
COUNTERS = (
    "playlists_added",
    "playlists_updated",
    "playlists_removed",
    "tracks_added",
    "tracks_updated",
    "tracks_removed",
)
ALLOWED_STATUSES = frozenset({"running"}) | TERMINAL


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


def _parse_timestamp(value: str) -> datetime:
    assert RFC3339.match(value), f"{value!r} is not RFC3339 with milliseconds"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validated(name: str, model: type[BaseModel]) -> Any:
    return model.model_validate(load_fixture(name))


def _run_fixture(name: str) -> dict[str, Any]:
    return load_fixture(name)


# --------------------------------------------------------------------------- #
# Shape and secrecy
# --------------------------------------------------------------------------- #


def test_fixture_set_is_exactly_the_p6_contract() -> None:
    assert {path.name for path in fixture_paths()} == EXPECTED_FIXTURES


def test_no_secret_markers_in_any_fixture() -> None:
    assert fixture_paths(), "no fixtures found"
    for path in fixture_paths():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in SECRET_MARKERS:
            assert marker not in lowered, f"{path.name} leaks {marker!r}"
        for key in _iter_keys(json.loads(text)):
            assert key.lower() not in FORBIDDEN_KEYS, (
                f"{path.name} contains forbidden field {key!r}"
            )


@pytest.mark.parametrize(("name", "model"), MODEL_FIXTURES.items(), ids=MODEL_FIXTURES.keys())
def test_fixture_matches_core_model(name: str, model: type[BaseModel]) -> None:
    payload = load_fixture(name)
    validated = model.model_validate(payload)
    # The fixture must survive the same JSON round-trip the API performs.
    assert json.loads(validated.model_dump_json()) == payload


# --------------------------------------------------------------------------- #
# SyncRun invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", RUN_FIXTURES)
def test_run_fixture_has_consistent_lifecycle(name: str) -> None:
    run = _validated(name, SyncRun)
    assert run.status in ALLOWED_STATUSES
    started = _parse_timestamp(run.started_at)

    if run.status == "running":
        assert run.finished_at is None
    else:
        assert run.finished_at is not None, f"{name}: a terminal run needs finished_at"
        assert _parse_timestamp(run.finished_at) >= started

    for counter in COUNTERS:
        assert getattr(run, counter) >= 0, f"{name}: {counter} must not be negative"

    if run.status == "success":
        assert run.error is None, f"{name}: success must not carry an error"
    if run.status in {"partial", "failed"}:
        assert run.error and run.error.strip(), f"{name}: {run.status} needs an error"


def test_partial_run_reports_a_playlist_error_without_secrets() -> None:
    run = _validated("run_partial.json", SyncRun)
    assert run.status == "partial"
    assert "1001:8" in run.error
    assert "timeout" in run.error
    for marker in SECRET_MARKERS:
        assert marker not in run.error.lower()


def test_repeat_run_is_idempotent_and_strictly_later() -> None:
    first = _validated("run_success.json", SyncRun)
    repeat = _validated("run_repeat.json", SyncRun)

    assert repeat.id > first.id
    assert _parse_timestamp(repeat.started_at) > _parse_timestamp(first.finished_at)
    # docs/SYNC.md §2.2: a repeated run on the same data changes nothing.
    assert all(getattr(repeat, counter) == 0 for counter in COUNTERS), repeat
    assert repeat.status == "success"
    # The first run did write data, otherwise the repeat proves nothing.
    assert first.playlists_added + first.tracks_added > 0


# --------------------------------------------------------------------------- #
# SyncStatus / SyncHistory coherence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ("status.json", "status_running.json"))
def test_status_fixture_keeps_the_documented_defaults(name: str) -> None:
    status = _validated(name, SyncStatus)
    assert status.interval_s == 3600, "docs/API.md §10: hourly interval"
    assert status.last is not None
    # last_attempt and last_success are separate: even a running sync keeps the
    # last finished run visible in status.
    assert status.last.finished_at is not None
    if status.running:
        assert status.progress, "a running sync should expose progress"
        assert "token" not in status.progress.lower()
    else:
        assert status.progress is None
        assert status.next_run_at is not None
        assert _parse_timestamp(status.next_run_at) > _parse_timestamp(
            status.last.finished_at
        )


def test_status_and_history_agree_on_the_newest_run() -> None:
    status = _validated("status.json", SyncStatus)
    history = _validated("history.json", SyncHistory)

    assert history.total == len(history.items) == 3
    assert history.items, "history must not be empty"
    assert status.last is not None
    assert status.last.model_dump() == history.items[0].model_dump()


def test_history_is_newest_first_with_unique_ids() -> None:
    history = _validated("history.json", SyncHistory)
    started = [_parse_timestamp(run.started_at) for run in history.items]
    assert started == sorted(started, reverse=True), "history must be newest first"
    ids = [run.id for run in history.items]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids, reverse=True)
    assert all(run.status in ALLOWED_STATUSES for run in history.items)


def test_history_items_match_the_standalone_run_fixtures() -> None:
    history = _validated("history.json", SyncHistory)
    expected = {
        run.id: run.model_dump()
        for run in (SyncRun.model_validate(_run_fixture(name)) for name in RUN_FIXTURES)
    }
    assert {run.id: run.model_dump() for run in history.items} == expected
    partial = next(run for run in history.items if run.status == "partial")
    assert partial.error


def test_history_total_is_the_documented_page_size() -> None:
    payload = load_fixture("history.json")
    assert set(payload) == {"items", "total"}
    # docs/API.md §10: history is a bounded page (default limit 20).
    assert 1 <= payload["total"] <= 20
    assert payload["total"] == len(payload["items"])
