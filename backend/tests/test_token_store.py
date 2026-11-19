"""TokenStore: atomic persistence, permissions, corruption and secret hygiene."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from app.integrations.yandex_music import token_store as token_store_module
from app.integrations.yandex_music.token_store import TokenData, TokenStore

ACCESS_TOKEN = "y0_AgAAAAB_access_secret_value"
REFRESH_TOKEN = "y0_AgAAAAB_refresh_secret_value"
STORE_LOGGER = "musicnx.yandex.token_store"


class _ListHandler(logging.Handler):
    """Collect formatted log messages without relying on root propagation."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def make_token(**overrides: object) -> TokenData:
    defaults: dict[str, object] = {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
        "obtained_at": 1_700_000_000.0,
        "token_type": "bearer",
        "login": "user@example",
        "source": "device",
    }
    defaults.update(overrides)
    return TokenData(**defaults)  # type: ignore[arg-type]


def test_save_creates_parent_directories_and_round_trips(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets" / "nested" / "yandex_token.json")
    token = make_token()

    store.save(token)

    assert store.path.is_file()
    assert store.load() == token


def test_saved_file_is_owner_only(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "yandex_token.json")

    store.save(make_token())

    mode = stat.S_IMODE(store.path.stat().st_mode)
    if os.name == "posix":
        assert mode == 0o600
    else:
        # Windows has no POSIX mode bits: the save must simply not fail.
        assert store.path.is_file()


def test_atomic_overwrite_leaves_no_temp_files(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "yandex_token.json")
    store.save(make_token(access_token="first-token"))
    store.save(make_token(access_token="second-token"))

    assert store.load() is not None
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "second-token"
    leftovers = list(tmp_path.glob(f".{store.path.name}.*.tmp"))
    assert leftovers == []


def test_save_uses_replace_of_a_temp_file_in_same_directory(tmp_path: Path, monkeypatch) -> None:
    store = TokenStore(tmp_path / "yandex_token.json")
    real_replace = os.replace
    calls: list[tuple[Path, Path]] = []

    def tracking_replace(source: object, target: object) -> None:
        calls.append((Path(str(source)), Path(str(target))))
        real_replace(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(token_store_module.os, "replace", tracking_replace)

    store.save(make_token())

    assert calls, "atomic write must go through os.replace"
    temp_path, final_path = calls[-1]
    assert final_path == store.path
    assert temp_path != store.path
    assert temp_path.parent == store.path.parent
    assert not temp_path.exists()


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    assert TokenStore(tmp_path / "absent.json").load() is None


def test_corrupted_file_returns_none_and_logs_no_token_content(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "yandex_token.json")
    store.path.write_text(
        '{"access_token": "' + ACCESS_TOKEN + '", "refresh_token": "broken",',
        encoding="utf-8",
    )
    handler = _ListHandler()
    logger = logging.getLogger(STORE_LOGGER)
    logger.addHandler(handler)
    try:
        assert store.load() is None
    finally:
        logger.removeHandler(handler)

    assert handler.messages
    assert all(ACCESS_TOKEN not in message for message in handler.messages)
    assert all(REFRESH_TOKEN not in message for message in handler.messages)


def test_invalid_payload_is_treated_as_corrupted(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "yandex_token.json")
    store.path.write_text(
        json.dumps({"access_token": ACCESS_TOKEN, "source": "hacked"}),
        encoding="utf-8",
    )

    assert store.load() is None


def test_repr_never_contains_secrets() -> None:
    token = make_token()

    rendered = repr(token)

    assert ACCESS_TOKEN not in rendered
    assert REFRESH_TOKEN not in rendered
    assert "***" in rendered


def test_expires_at_and_is_expired() -> None:
    token = make_token(expires_in=60, obtained_at=1_700_000_000.0)
    manual = make_token(expires_in=None, obtained_at=1_700_000_000.0)

    assert token.expires_at == 1_700_000_060.0
    assert token.is_expired(now=1_700_000_010.0) is False
    assert token.is_expired(now=1_700_000_060.0) is True
    assert manual.expires_at is None
    assert manual.is_expired(now=9_999_999_999.0) is False


def test_source_must_be_known() -> None:
    try:
        TokenData(access_token="token", source="other")  # type: ignore[arg-type]
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("unknown source must be rejected")


def test_clear_removes_file_and_is_idempotent(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "yandex_token.json")
    store.save(make_token())

    store.clear()
    assert not store.path.exists()
    store.clear()
    assert store.load() is None
