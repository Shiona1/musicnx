"""Import progress over the existing WebSocket bus."""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings


def test_import_progress_is_broadcast(tmp_path: Path, library_root: Path) -> None:
    app = create_app(
        make_settings(tmp_path, seed_catalog=False, import_roots=[library_root])
    )
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "snapshot"
        websocket.send_json({"type": "client.hello", "payload": {"visible": True}})

        assert client.post("/api/v1/library/import").status_code == 202

        events = []
        completed = False
        updated = False
        for _ in range(100):
            message = websocket.receive_json()
            events.append(message["type"])
            if message["type"] in {
                "library.import.completed",
                "library.import.failed",
                "library.import.cancelled",
            }:
                completed = True
                if message["type"] != "library.import.completed":
                    updated = True
            if message["type"] == "library.updated":
                updated = True
            if completed and updated:
                break
        assert "library.import.started" in events
        assert "library.import.progress" in events
        assert "library.import.completed" in events
        assert "library.updated" in events

        tracks = client.get("/api/v1/library/tracks").json()
        assert tracks["total"] == 4
