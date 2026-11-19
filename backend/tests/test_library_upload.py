"""Browser uploads (P2 extension): save, validate, never escape the root."""

from __future__ import annotations

import time
from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import make_settings

API = "/api/v1"
FIXTURE_MP3 = Path(__file__).parent / "fixtures" / "media" / "song_a.mp3"


def _client(
    tmp_path: Path,
    *,
    max_bytes: int = 4 * 1024 * 1024,
    quota_bytes: int = 2 * 1024 * 1024 * 1024,
    min_free_bytes: int = 512 * 1024 * 1024,
) -> TestClient:
    settings = make_settings(tmp_path, seed_catalog=False)
    settings.library.upload_max_bytes = max_bytes
    settings.library.upload_quota_bytes = quota_bytes
    settings.library.upload_min_free_bytes = min_free_bytes
    return TestClient(create_app(settings))


def _upload(client: TestClient, name: str, data: bytes, content_type: str = "audio/mpeg"):
    return client.post(
        f"{API}/library/upload",
        files={"file": (name, data, content_type)},
    )


def test_upload_saves_file_inside_library_root(tmp_path: Path) -> None:
    payload = b"ID3" + b"\x00" * 64
    with _client(tmp_path) as client:
        response = _upload(client, "My Track.mp3", payload)

    assert response.status_code == 201
    body = response.json()["file"]
    assert body["name"] == "My Track.mp3"
    assert body["size"] == len(payload)
    path = Path(body["path"])
    assert path.is_file()
    assert path.read_bytes() == payload
    assert path.parent.name == "uploads"


def test_upload_strips_path_traversal(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, "../../evil.mp3", b"data")

    assert response.status_code == 201
    name = response.json()["file"]["name"]
    assert name == "evil.mp3"
    assert Path(response.json()["file"]["path"]).parent.name == "uploads"


def test_upload_rejects_unsupported_extension(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, "notes.txt", b"hello")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_upload_rejects_empty_file(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, "empty.mp3", b"")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_upload_enforces_size_limit_and_cleans_partial(tmp_path: Path) -> None:
    with _client(tmp_path, max_bytes=1024) as client:
        response = _upload(client, "big.mp3", b"x" * 2048)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    uploads = tmp_path / "media" / "uploads"
    assert not list(uploads.glob("*")) if uploads.exists() else True


def test_duplicate_names_get_a_suffix(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = _upload(client, "same.mp3", b"one")
        second = _upload(client, "same.mp3", b"two")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["file"]["name"] == "same.mp3"
    assert second.json()["file"]["name"] == "same (1).mp3"


def test_upload_quota_rejects_and_cleans_partial(tmp_path: Path) -> None:
    with _client(tmp_path, quota_bytes=100) as client:
        response = _upload(client, "quota.mp3", b"x" * 200)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_quota_exceeded"
    uploads = tmp_path / "var" / "library" / "uploads"
    assert not uploads.exists() or not list(uploads.iterdir())


def test_upload_reserves_free_disk_space(tmp_path: Path) -> None:
    with _client(tmp_path, min_free_bytes=10**15) as client:
        response = _upload(client, "full.mp3", b"x" * 10)

    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_storage"


def test_uploaded_audio_is_imported_into_the_library(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = _upload(client, "uploaded.mp3", FIXTURE_MP3.read_bytes())
        assert response.status_code == 201
        path = response.json()["file"]["path"]

        started = client.post(f"{API}/library/import", json={"paths": [path]})
        assert started.status_code == 202
        assert started.json()["target_paths"] == [path]

        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            job = client.get(f"{API}/library/import/status").json()["job"]
            if job and job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)

        assert job is not None
        assert job["status"] == "completed", job
        assert job["imported"] == 1
        tracks = client.get(f"{API}/library/tracks", params={"search": "Song A"})
        assert tracks.status_code == 200
        assert tracks.json()["total"] >= 1
