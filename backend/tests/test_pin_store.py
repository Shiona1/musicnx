from __future__ import annotations

import asyncio
import json
import os
import stat

from app.auth import PinAttemptLimiter
from app.config import PathsSettings, SecuritySettings, Settings
from app.main import create_app
from app.pin_store import PinStore
from fastapi.testclient import TestClient


def _settings(tmp_path) -> Settings:
    data = tmp_path / "var"
    return Settings(
        mode="mock",
        paths=PathsSettings(data_dir=data, database=data / "musicnx.db"),
        security=SecuritySettings(pin="1234", session_secret="test-secret"),
    )


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_pin_store_hashes_and_persists(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = PinStore(settings)
    asyncio.run(store.set_pin("new-pin-5678"))

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert "new-pin-5678" not in store.path.read_text(encoding="utf-8")
    assert raw["version"] == 1
    if os.name != "nt":
        assert _mode(store.path) == 0o600

    reloaded = PinStore(settings)
    assert reloaded.verify("new-pin-5678") is True
    assert reloaded.verify("1234") is False
    assert reloaded.verify(None) is False


def test_pin_store_falls_back_to_config_before_change(tmp_path) -> None:
    settings = _settings(tmp_path)
    store = PinStore(settings)
    assert store.verify("1234") is True
    assert store.verify("0000") is False


def test_pin_attempt_limiter_persists_failures(tmp_path) -> None:
    path = tmp_path / "var" / "secrets" / "pin_attempts.json"
    settings = _settings(tmp_path).security
    first = PinAttemptLimiter(path)
    for offset in range(5):
        first.register_failure("192.0.2.10", settings, now=100.0 + offset)

    second = PinAttemptLimiter(path)
    assert second.is_blocked("192.0.2.10", settings, now=102.0) is True
    assert second.is_blocked("192.0.2.11", settings, now=102.0) is False
    assert "192.0.2.10" not in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert _mode(path) == 0o600

    second.reset("192.0.2.10")
    assert PinAttemptLimiter(path).is_blocked("192.0.2.10", settings, now=103.0) is False


def test_change_pin_api_requires_current_and_rotates(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.security.allowed_hosts.append("testserver")
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert login.status_code == 200

        bad = client.post(
            "/api/v1/auth/pin/change",
            json={"current_pin": "0000", "new_pin": "5678"},
        )
        assert bad.status_code == 401

        changed = client.post(
            "/api/v1/auth/pin/change",
            json={"current_pin": "1234", "new_pin": "5678"},
        )
        assert changed.status_code == 200
        assert changed.json()["changed"] is True

        client.cookies.clear()
        assert client.post("/api/v1/auth/pin", json={"pin": "1234"}).status_code == 401
        assert client.post("/api/v1/auth/pin", json={"pin": "5678"}).status_code == 200

    raw = (settings.paths.data_dir / "secrets" / "pin.json").read_text(encoding="utf-8")
    assert "5678" not in raw
