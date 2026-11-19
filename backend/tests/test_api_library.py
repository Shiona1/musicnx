"""Library read endpoints and mock catalog."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_tracks_page_contains_seeded_catalog(client: TestClient) -> None:
    payload = client.get("/api/v1/library/tracks").json()
    assert payload["total"] == 6
    assert payload["limit"] == 50
    assert len(payload["items"]) == 6
    first = payload["items"][0]
    assert first["availability"] == "online"
    assert first["has_online_source"] is True
    assert first["has_local_file"] is False
    assert first["artists"]
    assert first["album"]["title"]


def test_search_and_pagination(client: TestClient) -> None:
    payload = client.get("/api/v1/library/tracks", params={"search": "Ночной"}).json()
    assert payload["total"] == 1
    assert payload["items"][0]["title"] == "Ночной эфир"

    page = client.get("/api/v1/library/tracks", params={"limit": 2, "offset": 2}).json()
    assert page["limit"] == 2
    assert page["offset"] == 2
    assert len(page["items"]) == 2
    assert page["total"] == 6


def test_availability_filter(client: TestClient) -> None:
    online = client.get(
        "/api/v1/library/tracks", params={"availability": "online"}
    ).json()
    assert online["total"] == 6
    offline = client.get(
        "/api/v1/library/tracks", params={"availability": "offline"}
    ).json()
    assert offline["total"] == 0


def test_invalid_availability_is_422(client: TestClient) -> None:
    response = client.get(
        "/api/v1/library/tracks", params={"availability": "nonsense"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
