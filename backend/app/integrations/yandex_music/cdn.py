"""Choose a reachable Yandex CDN edge for a signed stream URL.

Yandex redirects a signed ``get-mp3`` / ``tracks_file_info`` URL to a regional
edge such as ``ext-strm-rukrd01seaix-03.strm.yandex.net``. The signature is
edge-independent, but individual edge hosts can be unreachable (ISP routing,
cache maintenance). The other edges in the same pool serve the same path.

This router probes the original host and its siblings, caches the first
reachable one for a few minutes and rewrites only the host — path, query and the
signature stay untouched. It is deliberately best-effort: when no probe
succeeds, the original URL is returned and the caller keeps the existing error
handling.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from app.logging_setup import get_logger

logger = get_logger("yandex.cdn")

CDN_TTL_S = 600.0
PROBE_TIMEOUT_S = 1.2
REDIRECT_TIMEOUT_S = 5.0
EDGE_RE = re.compile(
    r"^(?P<prefix>.+-)(?P<number>\d{2})(?P<suffix>\.strm\.yandex\.net)$",
    re.IGNORECASE,
)

Probe = Callable[[str], Awaitable[bool]]
RedirectResolver = Callable[[str], Awaitable[str | None]]


async def tcp_probe(host: str, port: int = 443, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """Return True when a TCP connection to the CDN edge can be opened."""

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_s
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):  # pragma: no cover - close races
        pass
    return True


async def resolve_redirect(url: str) -> str | None:
    """Return the CDN edge URL from a ``get-mp3`` 308 response."""

    timeout = aiohttp.ClientTimeout(total=REDIRECT_TIMEOUT_S, sock_connect=REDIRECT_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                url,
                headers={"Range": "bytes=0-0"},
                allow_redirects=False,
            ) as response,
        ):
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if location:
                    return urljoin(url, location)
    except (aiohttp.ClientError, TimeoutError):
        return None
    return None


class CdnRouter:
    def __init__(
        self,
        *,
        probe: Probe | None = None,
        redirect_resolver: RedirectResolver | None = None,
        ttl_s: float = CDN_TTL_S,
    ) -> None:
        self._probe = probe or tcp_probe
        self._resolve_redirect = redirect_resolver or resolve_redirect
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def rewrite(self, url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return url
        if host.endswith(".strm.yandex.net"):
            return await self._rewrite_edge(url)
        # ``get-mp3`` answers with a redirect to the actual edge; resolve it so
        # the caller (mpv or /stream) never follows an unreachable edge itself.
        if host == "api.music.yandex.net" and "/get-mp3/" in parsed.path:
            target = await self._resolve_redirect(url)
            if target:
                return await self._rewrite_edge(target)
        return url

    async def _rewrite_edge(self, url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        candidates = self._candidates(host)
        if len(candidates) == 1:
            return url

        chosen = await self._pick(host, candidates)
        if chosen == host:
            return url
        netloc = chosen if parsed.port is None else f"{chosen}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    async def _pick(self, host: str, candidates: list[str]) -> str:
        now = time.monotonic()
        cached = self._cache.get(host)
        if cached is not None and now < cached[1]:
            return cached[0]
        async with self._lock:
            cached = self._cache.get(host)
            if cached is not None and time.monotonic() < cached[1]:
                return cached[0]
            results = await asyncio.gather(
                *(self._probe(candidate) for candidate in candidates),
                return_exceptions=True,
            )
            chosen = host
            for candidate, result in zip(candidates, results, strict=True):
                if result is True:
                    chosen = candidate
                    break
            self._cache[host] = (chosen, time.monotonic() + self._ttl_s)
            if chosen != host:
                logger.warning(
                    "Yandex CDN edge %s is unreachable; using %s", host, chosen
                )
            return chosen

    @staticmethod
    def _candidates(host: str) -> list[str]:
        match = EDGE_RE.match(host)
        if match is None:
            return [host]
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        current = int(match.group("number"))
        # The original is first: a healthy edge must not be replaced.
        candidates = [host]
        for number in range(1, 10):
            if number == current:
                continue
            candidates.append(f"{prefix}{number:02d}{suffix}")
        return candidates


__all__ = ["CDN_TTL_S", "CdnRouter", "Probe", "tcp_probe"]
