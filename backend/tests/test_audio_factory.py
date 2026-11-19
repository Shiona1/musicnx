"""Backend selection: real mpv is POSIX-only in P1 (ADR-006)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.audio.factory import create_audio_backend
from app.config import AudioSettings
from app.errors import ConfigurationError
from tests.conftest import make_settings


def test_mock_mode_always_uses_mock_backend(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, mode="mock")
    settings.audio.backend = "mpv"
    assert create_audio_backend(settings).name == "mock"


def test_real_mpv_mode_on_non_posix_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "posix":  # pragma: no cover - only on Linux CI
        monkeypatch.setattr(os, "name", "nt")
    settings = make_settings(tmp_path, mode="real")
    settings.audio = AudioSettings(backend="mpv")
    with pytest.raises(ConfigurationError, match="POSIX"):
        create_audio_backend(settings)
