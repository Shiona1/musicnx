"""Server-side QR rendering as PNG data URIs.

Shared by the Yandex device-flow screens and the LAN share link. A rendering
failure degrades honestly: ``None`` means the UI shows the plain URL instead
of pretending a QR exists.
"""

from __future__ import annotations

import base64
import io

import qrcode

from app.logging_setup import get_logger

logger = get_logger("services.qr")


def qr_png_data_uri(payload: str) -> str | None:
    """PNG data-URI for a short public payload, or None when rendering fails."""

    try:
        image = qrcode.make(payload)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    except Exception:
        logger.warning("QR code could not be rendered: payload_len=%s", len(payload))
        return None
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
