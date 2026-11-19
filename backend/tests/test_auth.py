"""Mini-PIN: token signing, login cookies and auth status."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from app.auth import issue_session, validate_session, verify_pin
from app.config import SecuritySettings
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def _pin_client(
    tmp_path: Path,
    *,
    pin_max_attempts: int = 5,
    pin_lockout_s: float = 60.0,
) -> TestClient:
    return TestClient(
        create_app(
            make_settings(
                tmp_path,
                pin="1234",
                pin_max_attempts=pin_max_attempts,
                pin_lockout_s=pin_lockout_s,
            )
        )
    )


def test_pin_verification_is_exact() -> None:
    settings = SecuritySettings(pin="1234")
    assert verify_pin(settings, "1234") is True
    assert verify_pin(settings, "12345") is False
    assert verify_pin(settings, None) is False
    assert verify_pin(SecuritySettings(), "1234") is False


def test_session_roundtrip_and_tampering() -> None:
    settings = SecuritySettings(pin="1234", session_secret="unit-secret")
    token = issue_session(settings, now=1000)
    assert validate_session(token, settings, now=1001) is True
    assert validate_session(token + "x", settings, now=1001) is False
    assert validate_session(token, settings, now=1000 + settings.session_ttl_s + 1) is False
    assert validate_session(token, SecuritySettings(pin="4321"), now=1001) is False
    assert validate_session(None, settings) is False


def test_login_sets_hardened_cookie(tmp_path: Path) -> None:
    with _pin_client(tmp_path) as client:
        status = client.get("/api/v1/auth/status").json()
        assert status["pin_configured"] is True
        assert status["authenticated"] is False

        wrong = client.post("/api/v1/auth/pin", json={"pin": "0000"})
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "invalid_pin"

        response = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "musicnx_session=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/" in cookie

        status = client.get("/api/v1/auth/status").json()
        assert status["authenticated"] is True

        client.post("/api/v1/auth/logout")
        assert client.get("/api/v1/auth/status").json()["authenticated"] is False


def test_login_without_configured_pin_is_rejected(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "pin_not_configured"


def test_default_limit_blocks_sixth_attempt(tmp_path: Path) -> None:
    with _pin_client(tmp_path) as client:
        for _ in range(5):
            assert client.post("/api/v1/auth/pin", json={"pin": "0000"}).status_code == 401
        blocked = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "too_many_attempts"


def test_too_many_failed_pins_triggers_lockout(tmp_path: Path) -> None:
    with _pin_client(tmp_path, pin_max_attempts=2, pin_lockout_s=0.5) as client:
        for _ in range(2):
            wrong = client.post("/api/v1/auth/pin", json={"pin": "0000"})
            assert wrong.status_code == 401
            assert wrong.json()["error"]["code"] == "invalid_pin"

        blocked = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "too_many_attempts"
        assert "1234" not in blocked.text

        time.sleep(0.6)
        recovered = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert recovered.status_code == 200


def test_successful_pin_resets_failure_counter(tmp_path: Path) -> None:
    with _pin_client(tmp_path, pin_max_attempts=3) as client:
        for _ in range(2):
            assert client.post("/api/v1/auth/pin", json={"pin": "0000"}).status_code == 401

        assert client.post("/api/v1/auth/pin", json={"pin": "1234"}).status_code == 200

        for _ in range(2):
            assert client.post("/api/v1/auth/pin", json={"pin": "0000"}).status_code == 401
        # Two failures after the reset are below the limit; without the reset
        # this request would already see four failures and return 429.
        assert client.post("/api/v1/auth/pin", json={"pin": "1234"}).status_code == 200


@pytest.mark.parametrize("pin", ["", "123"])
def test_short_pin_is_rejected_by_config(pin: str) -> None:
    with pytest.raises(ValueError):
        SecuritySettings(pin=pin)
