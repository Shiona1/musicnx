"""Share-link endpoint: a QR the household can scan to open the web UI.

The Host header is the address the current browser used; a loopback host must
be swapped for the LAN address (or honestly flagged as phone-unreachable)
instead of rendering a useless localhost QR.
"""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def test_share_returns_url_and_qr(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/system/share")
        assert response.status_code == 200
        body = response.json()
        # TestClient sends Host: testserver — not loopback, passed through.
        assert body["url"] == "http://testserver"
        assert body["qr_data_uri"].startswith("data:image/png;base64,")
        assert body["loopback"] is False


def test_share_loopback_substitutes_lan_ip(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr("app.api.routes.system._lan_ip", lambda: "192.168.1.42")
        response = client.get(
            "/api/v1/system/share", headers={"Host": "127.0.0.1:8080"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["url"] == "http://192.168.1.42:8080"
        assert body["qr_data_uri"].startswith("data:image/png;base64,")
        assert body["loopback"] is False


def test_share_loopback_without_lan_ip_stays_honest(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr("app.api.routes.system._lan_ip", lambda: None)
        response = client.get(
            "/api/v1/system/share", headers={"Host": "127.0.0.1:8080"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["url"] == "http://127.0.0.1:8080"
        assert body["loopback"] is True
        # The QR is still rendered; the UI uses the flag to warn the user.
        assert body["qr_data_uri"].startswith("data:image/png;base64,")
