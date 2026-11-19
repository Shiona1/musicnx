"""System endpoints and adapter honesty."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["db"] == "ok"
    assert payload["mode"] == "mock"
    assert payload["version"]


def test_status_reports_mock_adapters_honestly(client: TestClient) -> None:
    payload = client.get("/api/v1/system/status").json()
    assert payload["mode"] == "mock"
    assert payload["audio"]["backend"] == "mock"
    assert payload["audio"]["ready"] is True
    assert payload["database"]["schema_version"] == 4
    assert payload["adapters"]["yandex"]["available"] is True
    assert payload["adapters"]["yandex"]["mode"] == "mock"
    assert payload["adapters"]["yandex"]["authenticated"] is False
    assert payload["adapters"]["bluetooth"]["adapter"] == "mock"
    assert payload["security"]["auth"] == "localhost_only"
    assert payload["security"]["pin_configured"] is False
    assert payload["player"]["status"] == "stopped"


def test_storage(client: TestClient) -> None:
    response = client.get("/api/v1/system/storage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"]["database"] is True
    assert payload["database_size_bytes"] > 0
    assert payload["disk"]["total_bytes"] > 0
    assert payload["paths"]["database"].endswith("musicnx.db")


def test_unknown_api_path_is_json_404(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_docs_are_available(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 200
