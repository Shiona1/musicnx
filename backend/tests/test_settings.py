"""P8 settings whitelist and maintenance timer (docs/API.md §12).

No network, no mpv, no Raspberry Pi: a temporary SQLite database, a fake player
and a small maintenance stub are enough.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.api import deps
from app.config import Settings
from app.core.models import SettingsSnapshot
from app.database.connection import Database
from app.database.repositories import SettingsRepository
from app.errors import AppError, UnavailableError
from app.main import create_app
from app.maintenance.service import MaintenanceService
from app.maintenance.timer import INTERVAL_KEY, MaintenanceTimer
from app.settings.service import (
    DEFAULT_VOLUME_KEY,
    DOWNLOAD_QUALITY_KEY,
    SYNC_ENABLED_KEY,
    SettingsService,
)
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"


class FakePlayer:
    """Records applied volumes instead of touching mpv/mock audio."""

    def __init__(self) -> None:
        self.volumes: list[int] = []

    async def set_volume(self, volume: int) -> None:
        self.volumes.append(volume)


class FakeMaintenance:
    """Stand-in for p8-a's maintenance service (``ctx.maintenance``)."""

    def __init__(self) -> None:
        self.calls = 0
        self.ran = asyncio.Event()

    async def run(self) -> None:
        self.calls += 1
        self.ran.set()


def _client(tmp_path: Path, *, pin: str | None = None) -> TestClient:
    """The real app (``main.py`` already mounts the settings router)."""

    return TestClient(create_app(make_settings(tmp_path, seed_catalog=False, pin=pin)))


# ---------------------------------------------------------------- service: read


async def test_snapshot_uses_config_defaults_and_hides_other_keys(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    await repository.set("player.volume", 55)
    await repository.set("yandex.access_token", "secret-token-value")
    service = SettingsService(settings, repository, player=FakePlayer())

    snapshot = await service.snapshot()

    assert isinstance(snapshot, SettingsSnapshot)
    assert snapshot.values == {
        DEFAULT_VOLUME_KEY: settings.player.default_volume,
        DOWNLOAD_QUALITY_KEY: settings.downloads.quality,
        SYNC_ENABLED_KEY: settings.sync.enabled,
    }
    assert snapshot.editable == [DEFAULT_VOLUME_KEY, DOWNLOAD_QUALITY_KEY]
    # Other rows in the settings table (session volume, tokens) never leak.
    assert "secret-token-value" not in snapshot.model_dump_json()
    assert "55" not in snapshot.model_dump_json()


async def test_snapshot_falls_back_when_stored_value_is_invalid(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    await repository.set(DEFAULT_VOLUME_KEY, 150)
    service = SettingsService(settings, repository)

    snapshot = await service.snapshot()

    assert snapshot.values[DEFAULT_VOLUME_KEY] == settings.player.default_volume


# --------------------------------------------------------------- service: write


async def test_update_applies_live_and_persists(settings: Settings, db: Database) -> None:
    repository = SettingsRepository(db)
    player = FakePlayer()
    service = SettingsService(settings, repository, player=player)

    snapshot = await service.update({DEFAULT_VOLUME_KEY: 35})

    assert player.volumes == [35]
    assert snapshot.values[DEFAULT_VOLUME_KEY] == 35
    assert await repository.get(DEFAULT_VOLUME_KEY) == 35


async def test_download_quality_applies_persists_and_survives_reload(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    service = SettingsService(settings, repository, player=FakePlayer())

    snapshot = await service.update({DOWNLOAD_QUALITY_KEY: "mp3"})
    assert snapshot.values[DOWNLOAD_QUALITY_KEY] == "mp3"
    assert settings.downloads.quality == "mp3"
    assert await repository.get(DOWNLOAD_QUALITY_KEY) == "mp3"

    # A reloaded service adopts the stored value on startup.
    settings.downloads.quality = "auto"
    reloaded = SettingsService(settings, repository, player=FakePlayer())
    await reloaded.startup()
    assert settings.downloads.quality == "mp3"

    with pytest.raises(AppError) as exc_info:
        await reloaded.update({DOWNLOAD_QUALITY_KEY: "flac-mp4"})
    assert exc_info.value.code == "validation_error"


async def test_value_survives_reload_and_is_applied_on_startup(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    await SettingsService(settings, repository, player=FakePlayer()).update(
        {DEFAULT_VOLUME_KEY: 22}
    )

    player = FakePlayer()
    reloaded = SettingsService(settings, repository, player=player)
    assert (await reloaded.snapshot()).values[DEFAULT_VOLUME_KEY] == 22

    await reloaded.startup()
    assert player.volumes == [22]


async def test_startup_without_stored_value_leaves_player_alone(
    settings: Settings, db: Database
) -> None:
    player = FakePlayer()
    await SettingsService(settings, SettingsRepository(db), player=player).startup()
    assert player.volumes == []


async def test_startup_with_invalid_stored_value_is_a_noop(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    await repository.set(DEFAULT_VOLUME_KEY, 101)
    player = FakePlayer()

    await SettingsService(settings, repository, player=player).startup()

    assert player.volumes == []


async def test_startup_without_player_does_not_fail(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    await repository.set(DEFAULT_VOLUME_KEY, 40)

    await SettingsService(settings, repository, player=None).startup()


@pytest.mark.parametrize("value", [101, -1, True, "40", 40.5, None, [40]])
async def test_invalid_volume_is_rejected_before_any_write(
    settings: Settings, db: Database, value: object
) -> None:
    repository = SettingsRepository(db)
    player = FakePlayer()
    service = SettingsService(settings, repository, player=player)

    with pytest.raises(AppError) as exc_info:
        await service.update({DEFAULT_VOLUME_KEY: value})

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "validation_error"
    assert player.volumes == []
    assert await repository.get(DEFAULT_VOLUME_KEY) is None


async def test_unknown_key_is_rejected(settings: Settings, db: Database) -> None:
    service = SettingsService(settings, SettingsRepository(db), player=FakePlayer())

    with pytest.raises(AppError) as exc_info:
        await service.update({"player.unknown": 1})

    assert exc_info.value.status_code == 422
    assert exc_info.value.details["unknown"] == ["player.unknown"]
    assert exc_info.value.details["editable"] == [
        DEFAULT_VOLUME_KEY,
        DOWNLOAD_QUALITY_KEY,
    ]


async def test_sync_enabled_is_reported_but_read_only(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    service = SettingsService(settings, repository)

    with pytest.raises(AppError) as exc_info:
        await service.update({SYNC_ENABLED_KEY: True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.details["read_only"] == [SYNC_ENABLED_KEY]
    assert "config.yaml" in exc_info.value.message
    # Not persisted: the scheduler still reads the config value.
    assert await repository.get(SYNC_ENABLED_KEY) is None


async def test_empty_patch_is_rejected(settings: Settings, db: Database) -> None:
    service = SettingsService(settings, SettingsRepository(db))

    with pytest.raises(AppError) as exc_info:
        await service.update({})

    assert exc_info.value.status_code == 422


async def test_unavailable_player_is_reported_honestly(
    settings: Settings, db: Database
) -> None:
    repository = SettingsRepository(db)
    service = SettingsService(settings, repository, player=object())

    with pytest.raises(UnavailableError) as exc_info:
        await service.update({DEFAULT_VOLUME_KEY: 30})

    assert exc_info.value.status_code == 503
    assert await repository.get(DEFAULT_VOLUME_KEY) is None


# ------------------------------------------------------------------ route: open


def test_get_settings_is_open_and_returns_contract(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(f"{API}/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["values"] == {
        DEFAULT_VOLUME_KEY: 70,
        DOWNLOAD_QUALITY_KEY: "auto",
        SYNC_ENABLED_KEY: False,
    }
    assert payload["editable"] == [DEFAULT_VOLUME_KEY, DOWNLOAD_QUALITY_KEY]


def test_get_settings_is_open_with_pin_configured(tmp_path: Path) -> None:
    with _client(tmp_path, pin="1234") as client:
        response = client.get(f"{API}/settings")

    assert response.status_code == 200


# ----------------------------------------------------------------- route: write


def test_put_settings_persists_and_applies(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put(f"{API}/settings", json={DEFAULT_VOLUME_KEY: 35})
        assert response.status_code == 200
        assert response.json()["values"][DEFAULT_VOLUME_KEY] == 35

        # Live application reaches the player state ...
        player = client.get(f"{API}/player/state")
        assert player.status_code == 200
        assert player.json()["volume"] == 35
        # ... and the value is visible on the next read.
        assert client.get(f"{API}/settings").json()["values"][DEFAULT_VOLUME_KEY] == 35


def test_put_settings_rejects_unknown_key(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put(f"{API}/settings", json={"nope": 1})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["unknown"] == ["nope"]


def test_put_settings_rejects_out_of_range_volume(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.put(f"{API}/settings", json={DEFAULT_VOLUME_KEY: 101}).status_code == 422
        volume = client.get(f"{API}/player/state").json()["volume"]
        assert volume == 70


@pytest.mark.skip(reason="PIN feature removed")
def test_put_settings_requires_pin_session(tmp_path: Path) -> None:
    with _client(tmp_path, pin="1234") as client:
        guarded = client.put(f"{API}/settings", json={DEFAULT_VOLUME_KEY: 50})
        assert guarded.status_code == 401
        assert guarded.json()["error"]["code"] == "auth_required"

        assert client.post(f"{API}/auth/pin", json={"pin": "1234"}).status_code == 200
        allowed = client.put(f"{API}/settings", json={DEFAULT_VOLUME_KEY: 50})
        assert allowed.status_code == 200
        assert allowed.json()["values"][DEFAULT_VOLUME_KEY] == 50


def test_put_settings_survives_an_app_restart(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.put(f"{API}/settings", json={DEFAULT_VOLUME_KEY: 28}).status_code == 200

    # A second application instance opens the same database: startup applies the
    # stored default again, so both the API value and the player are consistent.
    with _client(tmp_path) as client:
        assert client.get(f"{API}/settings").json()["values"][DEFAULT_VOLUME_KEY] == 28
        assert client.get(f"{API}/player/state").json()["volume"] == 28


def test_app_wires_service_and_timer_is_disabled_by_default(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        context = client.app.state.ctx

    assert context.settings_service is not None
    assert context.maintenance_timer.is_running is False


def test_put_settings_rejects_non_object_body(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put(f"{API}/settings", json=[1, 2])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.skip(reason="PIN feature removed")
def test_put_settings_without_pin_is_localhost_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "client_is_loopback", lambda request: False)
    with _client(tmp_path) as client:
        response = client.put(f"{API}/settings", json={DEFAULT_VOLUME_KEY: 50})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "pin_required"


# ------------------------------------------------------------------ maintenance


async def test_timer_is_disabled_by_default(db: Database) -> None:
    maintenance = FakeMaintenance()
    timer = MaintenanceTimer(SettingsRepository(db), maintenance)

    assert await timer.start() is False
    assert timer.is_running is False
    assert maintenance.calls == 0


@pytest.mark.parametrize("value", ["daily", 0, -30, True])
async def test_timer_treats_invalid_interval_as_disabled(
    db: Database, value: object
) -> None:
    repository = SettingsRepository(db)
    await repository.set(INTERVAL_KEY, value)
    timer = MaintenanceTimer(repository, FakeMaintenance())

    assert await timer.start() is False
    assert timer.is_running is False


async def test_timer_starts_and_stops_without_firing_early(db: Database) -> None:
    repository = SettingsRepository(db)
    await repository.set(INTERVAL_KEY, 3600)
    maintenance = FakeMaintenance()
    timer = MaintenanceTimer(repository, maintenance)

    assert await timer.start() is True
    assert timer.is_running is True
    assert await timer.start() is True  # idempotent

    await timer.stop()
    assert timer.is_running is False
    assert maintenance.calls == 0  # the first run is one interval away
    await timer.stop()  # idempotent


async def test_timer_runs_maintenance_on_tick(db: Database) -> None:
    repository = SettingsRepository(db)
    await repository.set(INTERVAL_KEY, 0.01)
    maintenance = FakeMaintenance()
    timer = MaintenanceTimer(repository, maintenance, min_interval_s=0.01)

    assert await timer.start() is True
    try:
        await asyncio.wait_for(maintenance.ran.wait(), timeout=2.0)
    finally:
        await timer.stop()

    assert maintenance.calls >= 1


async def test_timer_without_service_is_a_noop(db: Database) -> None:
    timer = MaintenanceTimer(SettingsRepository(db))

    assert await timer.run_once() is False


async def test_timer_accepts_plain_callable(db: Database) -> None:
    calls: list[int] = []

    async def run() -> None:
        calls.append(1)

    timer = MaintenanceTimer(SettingsRepository(db), run)

    assert await timer.run_once() is True
    assert calls == [1]


async def test_timer_calls_the_p8_maintenance_service(
    settings: Settings, db: Database
) -> None:
    timer = MaintenanceTimer(
        SettingsRepository(db), MaintenanceService(settings, db)
    )

    assert await timer.run_once() is True
    assert await timer.run_once() is True

