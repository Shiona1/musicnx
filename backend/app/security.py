"""Host and Origin validation for the LAN threat model.

A request is accepted when the hostname is on the allowlist, is a private or
loopback IP address, or ends with ``.local`` (mDNS). This blocks DNS-rebinding
through attacker-controlled names while keeping the device usable by LAN IP.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from app.config import SecuritySettings


def hostname_from_authority(value: str | None) -> str | None:
    """Extract a lowercase hostname from a Host header (supports IPv6)."""

    if not value:
        return None
    parsed = urlparse(f"//{value}")
    return parsed.hostname.lower() if parsed.hostname else None


def is_allowed_hostname(hostname: str | None, settings: SecuritySettings) -> bool:
    if not hostname:
        return False
    normalized = hostname.lower().rstrip(".")
    allowed = {host.lower().rstrip(".") for host in settings.allowed_hosts}
    if normalized in allowed:
        return True
    if normalized.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def is_allowed_origin(origin: str | None, settings: SecuritySettings) -> bool:
    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    return is_allowed_hostname(parsed.hostname, settings)
