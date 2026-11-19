"""Secret redaction in logs."""

from __future__ import annotations

import logging

from app.logging_setup import RedactionFilter, redact


def test_redact_token_values() -> None:
    text = (
        "access_token=abc123 refresh_token: def456 "
        "password=hunter2 authorization=Bearer%20xyz pin=1234"
    )
    cleaned = redact(text)
    for secret in ("abc123", "def456", "hunter2", "Bearer%20xyz", "1234"):
        assert secret not in cleaned
    assert "***" in cleaned


def test_redact_query_parameters() -> None:
    cleaned = redact("GET https://cdn.example/audio?token=sig123&sig=sign456&x=1")
    assert "sig123" not in cleaned
    assert "sign456" not in cleaned
    assert "x=1" in cleaned


def test_filter_redacts_arguments() -> None:
    record = logging.LogRecord(
        name="musicnx.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="oauth token=%s",
        args=("super-secret",),
        exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    assert "super-secret" not in record.getMessage()
    assert "***" in record.getMessage()


def test_redaction_keeps_normal_text() -> None:
    assert redact("player started in mock mode") == "player started in mock mode"
