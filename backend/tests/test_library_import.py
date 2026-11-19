"""P2 import pipeline, dedupe, idempotence, verification and missing files."""

from __future__ import annotations

import time
from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

TERMINAL = {"completed", "failed", "cancelled"}


def _client(tmp_path: Path, root: Path) -> TestClient:
    return TestClient(
        create_app(make_settings(tmp_path, seed_catalog=False, import_roots=[root]))
    )


def _wait_job(client: TestClient, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get("/api/v1/library/import/status").json()
        job = payload.get("job")
        if job and job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    raise AssertionError("import job did not finish")


def test_import_pipeline_dedupes_and_verifies(tmp_path: Path, library_root: Path) -> None:
    with _client(tmp_path, library_root) as client:
        response = client.post("/api/v1/library/import")
        assert response.status_code == 202
        job = _wait_job(client)
        assert job["status"] == "completed", job
        assert job["total"] == 5
        assert job["imported"] == 4  # 5 files, MP3+FLAC share one track
        assert job["updated"] == 1
        assert job["missing"] == 0
        assert job["verify_processed"] == 5

        tracks = client.get("/api/v1/library/tracks").json()
        assert tracks["total"] == 4
        shared = [
            item for item in tracks["items"] if item["title"] == "Shared Song"
        ]
        assert len(shared) == 2
        by_album = {item["album"]["title"]: item for item in shared}
        assert set(by_album) == {"Test Album", "Other Album"}
        assert all(item["availability"] == "offline" for item in tracks["items"])
        assert all(item["has_local_file"] for item in tracks["items"])

        albums = client.get("/api/v1/library/albums").json()
        assert albums["total"] == 4
        artists = client.get("/api/v1/library/artists").json()
        assert artists["total"] == 3

        # Cover cache was filled for tagged albums and is served over HTTP.
        covered = [item for item in tracks["items"] if item["cover_url"]]
        assert len(covered) == 3
        artwork = client.get(covered[0]["cover_url"])
        assert artwork.status_code == 200
        assert artwork.headers["content-type"] == "image/jpeg"
        assert "immutable" in artwork.headers["cache-control"]
        small = client.get(covered[0]["cover_url"].replace("size=300", "size=64"))
        assert small.status_code == 200
        bad_size = client.get(covered[0]["cover_url"].replace("size=300", "size=999"))
        assert bad_size.status_code == 422


def test_import_is_idempotent(tmp_path: Path, library_root: Path) -> None:
    with _client(tmp_path, library_root) as client:
        assert client.post("/api/v1/library/import").status_code == 202
        first = _wait_job(client)
        assert first["status"] == "completed"
        tracks_before = client.get("/api/v1/library/tracks").json()["total"]
        assert client.post("/api/v1/library/import").status_code == 202
        second = _wait_job(client)
        assert second["status"] == "completed"
        assert second["imported"] == 0
        assert second["updated"] == 0
        assert second["skipped"] == 5
        assert client.get("/api/v1/library/tracks").json()["total"] == tracks_before


def test_missing_file_marks_track_without_deleting_it(
    tmp_path: Path, library_root: Path
) -> None:
    with _client(tmp_path, library_root) as client:
        client.post("/api/v1/library/import")
        assert _wait_job(client)["status"] == "completed"
        (library_root / "song_b.flac").unlink()

        client.post("/api/v1/library/import")
        job = _wait_job(client)
        assert job["missing"] == 1

        tracks = client.get("/api/v1/library/tracks").json()["items"]
        missing = [item for item in tracks if item["title"] == "Shared Song"]
        targets = [
            item
            for item in missing
            if item["album"]["title"] == "Other Album"
        ]
        assert len(targets) == 1
        assert targets[0]["availability"] == "missing"
        assert targets[0]["file_missing"] is True
        assert targets[0]["has_local_file"] is False


def test_missing_sibling_file_keeps_track_offline(
    tmp_path: Path, library_root: Path
) -> None:
    with _client(tmp_path, library_root) as client:
        client.post("/api/v1/library/import")
        assert _wait_job(client)["status"] == "completed"
        (library_root / "song_a.mp3").unlink()

        client.post("/api/v1/library/import")
        job = _wait_job(client)
        assert job["missing"] == 1

        items = client.get("/api/v1/library/tracks").json()["items"]
        targets = [
            item
            for item in items
            if item["title"] == "Shared Song" and item["album"]["title"] == "Test Album"
        ]
        assert len(targets) == 1
        assert targets[0]["availability"] == "offline"
        assert targets[0]["file_missing"] is False
        assert targets[0]["has_local_file"] is True


def test_missing_cover_file_is_rerendered(tmp_path: Path, library_root: Path) -> None:
    settings = make_settings(
        tmp_path, seed_catalog=False, import_roots=[library_root]
    )
    with TestClient(create_app(settings)) as client:
        client.post("/api/v1/library/import")
        assert _wait_job(client)["status"] == "completed"
        covered = [
            item
            for item in client.get("/api/v1/library/tracks").json()["items"]
            if item["cover_url"]
        ]
        assert covered
        key = covered[0]["cover_url"].split("/artwork/", 1)[1].split("?", 1)[0]
        cached = settings.paths.cache / "covers" / f"{key}-300.jpg"
        assert cached.is_file()
        cached.unlink()

        client.post("/api/v1/library/import")
        assert _wait_job(client)["status"] == "completed"
        assert cached.is_file()


def test_import_rejects_path_outside_roots(tmp_path: Path, library_root: Path) -> None:
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"not audio")
    with _client(tmp_path, library_root) as client:
        response = client.post("/api/v1/library/import", json={"paths": [str(outside)]})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "path_not_allowed"


def test_import_continues_after_corrupt_file(
    tmp_path: Path, library_root: Path
) -> None:
    (library_root / "broken.mp3").write_text("not an mp3", encoding="utf-8")
    with _client(tmp_path, library_root) as client:
        client.post("/api/v1/library/import")
        job = _wait_job(client)
        assert job["status"] == "completed"
        assert job["imported"] == 4
        assert job["errors"]
        assert "broken.mp3" in job["errors"][0]


def test_cancel_without_job_conflicts(tmp_path: Path, library_root: Path) -> None:
    with _client(tmp_path, library_root) as client:
        response = client.post("/api/v1/library/import/cancel")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "import_not_running"
