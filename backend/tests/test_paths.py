"""Download path template: sanitisation, unicode, traversal."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.downloads.paths import (
    DEFAULT_PATH_TEMPLATE,
    PathContext,
    PathTemplateError,
    ensure_within_root,
    render_relative_path,
    sanitize_component,
)


def context(**overrides) -> PathContext:
    values = dict(
        artists="Тестовый Оркестр",
        album="Mock Album",
        year=2024,
        disc=1,
        track=1,
        title="Ночной эфир",
        ext="flac",
    )
    values.update(overrides)
    return PathContext(**values)


def test_default_template_renders_expected_layout() -> None:
    path = render_relative_path(DEFAULT_PATH_TEMPLATE, context())
    assert path == Path("Тестовый Оркестр/Mock Album (2024)/Disc 1/01 - Ночной эфир.flac")


def test_missing_year_cleans_empty_parentheses() -> None:
    path = render_relative_path(DEFAULT_PATH_TEMPLATE, context(year=None))
    assert "()" not in str(path)
    assert path.parts[1] == "Mock Album"


def test_extension_is_normalised() -> None:
    path = render_relative_path("{title}{ext}", context(ext=".FLAC"))
    assert path == Path("Ночной эфир.flac")
    path = render_relative_path("{title}{ext}", context(ext="m4a"))
    assert path == Path("Ночной эфир.m4a")


def test_illegal_characters_are_replaced() -> None:
    path = render_relative_path("{artists}/{title}{ext}", context(artists="AC/DC", title="A:B*C?"))
    assert path.parts[0] == "AC_DC"
    assert path.parts[1] == "A_B_C_.flac" or path.parts[1].startswith("A_B_C")


def test_slash_in_title_does_not_create_directories() -> None:
    path = render_relative_path("{title}{ext}", context(title="AC/DC - Live"))
    assert len(path.parts) == 1
    assert "/" not in path.name


def test_dot_dot_template_is_rejected() -> None:
    with pytest.raises(PathTemplateError):
        render_relative_path("../{title}", context())


def test_absolute_template_is_rejected() -> None:
    with pytest.raises(PathTemplateError):
        render_relative_path("/{title}", context())


def test_unknown_placeholder_is_rejected() -> None:
    with pytest.raises(PathTemplateError):
        render_relative_path("{unknown}", context())


def test_windows_reserved_name_gets_suffix() -> None:
    assert sanitize_component("CON") == "CON_"
    assert sanitize_component("nul") == "nul_"


def test_long_component_is_truncated_keeping_extension() -> None:
    long_title = "Ж" * 300
    path = render_relative_path("{title}{ext}", context(title=long_title))
    name = path.name
    assert len(name) <= 120
    assert name.endswith(".flac")


def test_sanitize_falls_back_for_empty_value() -> None:
    assert sanitize_component("   ") == "Unknown"
    assert sanitize_component("..") == "Unknown"


def test_ensure_within_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    with pytest.raises(PathTemplateError):
        ensure_within_root(root, Path("..") / "escape.txt")
    inside = ensure_within_root(root, Path("Artist/track.flac"))
    assert inside.is_relative_to(root.resolve())
