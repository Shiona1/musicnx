"""Host/Origin protection and security headers."""

from __future__ import annotations

from app.config import SecuritySettings
from app.security import hostname_from_authority, is_allowed_hostname, is_allowed_origin
from fastapi.testclient import TestClient


def test_hostname_parsing() -> None:
    assert hostname_from_authority("musicnx.local:8080") == "musicnx.local"
    assert hostname_from_authority("192.168.1.10:8080") == "192.168.1.10"
    assert hostname_from_authority("[::1]:8080") == "::1"
    assert hostname_from_authority(None) is None


def test_allowed_hostnames() -> None:
    settings = SecuritySettings(allowed_hosts=["musicnx.local"])
    assert is_allowed_hostname("musicnx.local", settings)
    assert is_allowed_hostname("livingroom.local", settings)
    assert is_allowed_hostname("127.0.0.1", settings)
    assert is_allowed_hostname("192.168.0.42", settings)
    assert is_allowed_hostname("::1", settings)
    assert not is_allowed_hostname("evil.example", settings)
    assert not is_allowed_hostname("8.8.8.8", settings)
    assert not is_allowed_hostname(None, settings)


def test_origin_validation() -> None:
    settings = SecuritySettings()
    assert is_allowed_origin("http://musicnx.local:8080", settings)
    assert is_allowed_origin("https://192.168.1.10", settings)
    assert not is_allowed_origin("http://evil.example", settings)
    assert not is_allowed_origin("null", settings)
    assert not is_allowed_origin(None, settings)


def test_bad_host_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/v1/system/health", headers={"host": "evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "host_forbidden"


def test_bad_origin_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/api/v1/system/health", headers={"origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_forbidden"


def test_allowed_origin_passes(client: TestClient) -> None:
    response = client.get(
        "/api/v1/system/health", headers={"origin": "http://testserver"}
    )
    assert response.status_code == 200


def test_security_headers(client: TestClient) -> None:
    response = client.get("/api/v1/system/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
