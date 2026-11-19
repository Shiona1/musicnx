"""Logging setup with size-based rotation and secret redaction."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from app.config import LoggingSettings

_LOGGER_NAME = "musicnx"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(access_token|refresh_token|token|password|secret|authorization|pin)"
        r"(\s*[:=]\s*[\"']?)([^\s,;\"'}&]+)"
    ),
    re.compile(r"(?i)([?&](?:access_token|token|sig|signature)=)([^&\s]+)"),
)


def redact(text: str) -> str:
    """Replace values of secret-looking keys with ``***``."""

    result = _SECRET_PATTERNS[0].sub(
        lambda match: f"{match.group(1)}{match.group(2)}***", text
    )
    return _SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}***", result)


class RedactionFilter(logging.Filter):
    """Redact secrets from messages and arguments before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            try:
                rendered = record.getMessage()
            except Exception:  # pragma: no cover - malformed format string
                rendered = str(record.msg)
            record.msg = redact(rendered)
            record.args = ()
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def setup_logging(settings: LoggingSettings) -> logging.Logger:
    """Configure the ``musicnx`` logger once per process.

    The logger does not propagate to the root logger: uvicorn keeps its own
    handlers and tests can attach ``caplog`` to the root without interference.
    """

    logger = logging.getLogger(_LOGGER_NAME)
    if getattr(logger, "_musicnx_configured", False):
        logger.setLevel(settings.level)
        return logger

    logger.setLevel(settings.level)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(RedactionFilter())
    logger.addHandler(console)

    if settings.file is not None:
        try:
            settings.file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                settings.file,
                maxBytes=settings.max_bytes,
                backupCount=settings.backup_count,
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - depends on the environment
            logger.warning("cannot open log file %s: %s; logging to console", settings.file, exc)
        else:
            file_handler.setFormatter(formatter)
            file_handler.addFilter(RedactionFilter())
            logger.addHandler(file_handler)

    logger._musicnx_configured = True  # type: ignore[attr-defined]
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child of the MusicNX logger."""

    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
