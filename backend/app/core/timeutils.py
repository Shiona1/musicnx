"""Time helpers: all timestamps are UTC ISO-8601 with milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return to_iso(utc_now())


def to_iso(value: datetime) -> str:
    utc_value = value.astimezone(UTC)
    millis = utc_value.microsecond // 1000
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"
