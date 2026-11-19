"""Config precedence: environment over YAML over defaults."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from app.config import AudioSettings, Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.mode == "mock"
    assert settings.server.port == 8080
    assert settings.mock.seed_catalog is True
    assert settings.player.position_persist_interval_s == 7.0
    assert settings.logging.file is not None


def test_yaml_is_loaded(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "mode": "real",
                "server": {"port": 9001},
                "logging": {"level": "debug"},
                "mock": {"seed_catalog": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MUSICNX_CONFIG", str(config_path))
    monkeypatch.delenv("MUSICNX_MODE", raising=False)
    monkeypatch.delenv("MUSICNX_SERVER__PORT", raising=False)

    settings = Settings()
    assert settings.mode == "real"
    assert settings.server.port == 9001
    assert settings.logging.level == "DEBUG"
    assert settings.mock.seed_catalog is False


def test_environment_overrides_yaml(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"server": {"port": 9001}, "logging": {"level": "info"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MUSICNX_CONFIG", str(config_path))
    monkeypatch.setenv("MUSICNX_SERVER__PORT", "9100")
    monkeypatch.setenv("MUSICNX_LOGGING__LEVEL", "warning")

    settings = Settings()
    assert settings.server.port == 9100
    assert settings.logging.level == "WARNING"


def test_missing_yaml_is_not_an_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MUSICNX_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("MUSICNX_MODE", raising=False)
    settings = Settings()
    assert settings.mode == "mock"


def test_allowed_hosts_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MUSICNX_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv(
        "MUSICNX_SECURITY__ALLOWED_HOSTS",
        '["musicnx.lan","192.168.1.10"]',
    )
    settings = Settings()
    assert settings.security.allowed_hosts == ["musicnx.lan", "192.168.1.10"]


def test_ipc_socket_uses_xdg_runtime_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    settings = AudioSettings()
    assert settings.ipc_socket == tmp_path / "musicnx" / "mpv.sock"


def test_ipc_socket_falls_back_to_uid_runtime_dir(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    settings = AudioSettings()
    assert settings.ipc_socket == Path("/run/user/1234/musicnx/mpv.sock")


def test_pin_limiter_defaults() -> None:
    settings = Settings()
    assert settings.security.pin_max_attempts == 5
    assert settings.security.pin_lockout_s == 60.0
