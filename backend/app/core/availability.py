"""Availability badge derived from independent flags (SPEC §6).

A track can be both offline and online; the badge shows the most important
state. The priority is documented in docs/DATABASE.md §4.2.
"""

from __future__ import annotations

from app.core.models import Availability


def compute_availability(
    *,
    has_local_file: bool,
    has_online_source: bool,
    file_missing: bool,
    source_unavailable: bool,
) -> Availability:
    """Return the UI badge for a track.

    Priority: missing > offline > unavailable > online > metadata_only.
    """

    if file_missing:
        return "missing"
    if has_local_file:
        return "offline"
    if has_online_source and source_unavailable:
        return "unavailable"
    if has_online_source:
        return "online"
    return "metadata_only"
