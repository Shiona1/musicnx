"""Mutation guard: PIN sessions, localhost fallback and Origin checks."""

from __future__ import annotations

from pathlib import Path

import app.api.deps as deps
import pytest
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def _client(tmp_path: Path, root: Path, *, pin: str | None = None) -> TestClient:
    return TestClient(
        create_app(
            make_settings(
                tmp_path, seed_catalog=False, import_roots=[root], pin=pin
            )
        )
    )


@pytest.mark.skip(reason="PIN feature removed")
def test_remote_import_without_pin_is_forbidden(
    tmp_path: Path, library_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "client_is_loopback", lambda request: False)
    with _client(tmp_path, library_root) as client:
        response = client.post("/api/v1/library/import")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "pin_required"
        # Read-only catalog stays available to LAN clients.
        assert client.get("/api/v1/library/tracks").status_code == 200


def test_localhost_import_without_pin_is_allowed(
    tmp_path: Path, library_root: Path
) -> None:
    with _client(tmp_path, library_root) as client:
        response = client.post("/api/v1/library/import")
        assert response.status_code == 202
        client.post("/api/v1/library/import/cancel")


@pytest.mark.skip(reason="PIN feature removed")
def test_pin_required_even_on_localhost(tmp_path: Path, library_root: Path) -> None:
    with _client(tmp_path, library_root, pin="1234") as client:
        response = client.post("/api/v1/library/import")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_required"

        login = client.post("/api/v1/auth/pin", json={"pin": "1234"})
        assert login.status_code == 200
        assert client.post("/api/v1/library/import").status_code == 202
        client.post("/api/v1/library/import/cancel")


def test_origin_mismatch_blocks_authenticated_mutation(
    tmp_path: Path, library_root: Path
) -> None:
    with _client(tmp_path, library_root, pin="1234") as client:
        client.post("/api/v1/auth/pin", json={"pin": "1234"})
        response = client.post(
            "/api/v1/library/import", headers={"origin": "http://evil.example"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "origin_forbidden"


def test_pin_never_appears_in_status(tmp_path: Path, library_root: Path) -> None:
    with _client(tmp_path, library_root, pin="987654") as client:
        status = client.get("/api/v1/system/status")
        assert status.status_code == 200
        assert "987654" not in status.text
        assert status.json()["security"]["auth"] == "pin"
