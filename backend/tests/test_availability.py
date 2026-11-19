"""Availability badge computation."""

from __future__ import annotations

import pytest
from app.core.availability import compute_availability


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            dict(
                has_local_file=True,
                has_online_source=True,
                file_missing=False,
                source_unavailable=False,
            ),
            "offline",
        ),
        (
            dict(
                has_local_file=False,
                has_online_source=True,
                file_missing=False,
                source_unavailable=False,
            ),
            "online",
        ),
        (
            dict(
                has_local_file=True,
                has_online_source=True,
                file_missing=True,
                source_unavailable=False,
            ),
            "missing",
        ),
        (
            dict(
                has_local_file=False,
                has_online_source=True,
                file_missing=False,
                source_unavailable=True,
            ),
            "unavailable",
        ),
        (
            dict(
                has_local_file=False,
                has_online_source=False,
                file_missing=False,
                source_unavailable=False,
            ),
            "metadata_only",
        ),
        (
            dict(
                has_local_file=True,
                has_online_source=True,
                file_missing=False,
                source_unavailable=True,
            ),
            "offline",
        ),
    ],
)
def test_availability_priority(flags: dict[str, bool], expected: str) -> None:
    assert compute_availability(**flags) == expected
