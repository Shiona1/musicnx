"""Queue REST endpoints and current-item behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _ids(client: TestClient) -> list[int]:
    return [item["id"] for item in client.get("/api/v1/library/tracks").json()["items"]]


def test_queue_is_empty_initially(client: TestClient) -> None:
    queue = client.get("/api/v1/queue").json()
    assert queue["items"] == []
    assert queue["total"] == 0
    assert queue["current_item_id"] is None
    assert queue["total_duration_ms"] == 0


def test_add_duplicates_and_delete_renumbers(client: TestClient) -> None:
    track_id = _ids(client)[0]
    response = client.post(
        "/api/v1/queue", json={"track_ids": [track_id, track_id, track_id]}
    )
    assert response.status_code == 201
    queue = response.json()
    assert queue["total"] == 3
    assert [item["position"] for item in queue["items"]] == [0, 1, 2]
    assert len({item["id"] for item in queue["items"]}) == 3

    removed = queue["items"][1]["id"]
    queue = client.delete(f"/api/v1/queue/{removed}").json()
    assert queue["total"] == 2
    assert [item["position"] for item in queue["items"]] == [0, 1]
    assert removed not in [item["id"] for item in queue["items"]]


def test_add_unknown_track_is_atomic(client: TestClient) -> None:
    track_id = _ids(client)[0]
    response = client.post("/api/v1/queue", json={"track_ids": [track_id, 99999]})
    assert response.status_code == 404
    assert client.get("/api/v1/queue").json()["total"] == 0


def test_empty_add_is_validation_error(client: TestClient) -> None:
    response = client.post("/api/v1/queue", json={"track_ids": []})
    assert response.status_code == 422


def test_reorder_requires_permutation(client: TestClient) -> None:
    ids = _ids(client)[:3]
    queue = client.post("/api/v1/queue", json={"track_ids": ids}).json()
    item_ids = [item["id"] for item in queue["items"]]

    bad = client.post("/api/v1/queue/reorder", json={"item_ids": item_ids[:2]})
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "queue_changed"

    good = client.post(
        "/api/v1/queue/reorder", json={"item_ids": list(reversed(item_ids))}
    )
    assert good.status_code == 200
    assert [item["id"] for item in good.json()["items"]] == list(reversed(item_ids))


def test_removing_current_item_selects_next_and_stops(client: TestClient) -> None:
    ids = _ids(client)[:2]
    queue = client.post("/api/v1/queue", json={"track_ids": ids}).json()
    first_item = queue["items"][0]
    client.post("/api/v1/player/play", json={"track_id": ids[0]})

    queue = client.delete(f"/api/v1/queue/{first_item['id']}").json()
    assert queue["total"] == 1
    state = client.get("/api/v1/player/state").json()
    assert state["status"] == "stopped"
    assert state["queue_item_id"] == queue["items"][0]["id"]
    assert state["track"]["id"] == ids[1]


def test_removing_last_item_clears_current(client: TestClient) -> None:
    track_id = _ids(client)[0]
    queue = client.post("/api/v1/queue", json={"track_ids": [track_id]}).json()
    client.post("/api/v1/player/play", json={"track_id": track_id})
    client.delete(f"/api/v1/queue/{queue['items'][0]['id']}")
    state = client.get("/api/v1/player/state").json()
    assert state["status"] == "stopped"
    assert state["track"] is None
    assert state["queue_item_id"] is None
