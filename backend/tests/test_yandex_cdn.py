"""Yandex CDN edge fallback: rewrite a signed URL to a reachable sibling."""

from __future__ import annotations

import asyncio

from app.integrations.yandex_music.cdn import CdnRouter


def test_candidates_keep_original_first() -> None:
    candidates = CdnRouter._candidates("ext-strm-rukrd01seaix-03.strm.yandex.net")
    assert candidates[0] == "ext-strm-rukrd01seaix-03.strm.yandex.net"
    assert "ext-strm-rukrd01seaix-02.strm.yandex.net" in candidates
    assert len(candidates) == 9  # original + 8 siblings
    # A non-edge host is never rewritten.
    assert CdnRouter._candidates("api.music.yandex.net") == ["api.music.yandex.net"]


async def test_rewrite_switches_to_reachable_sibling_and_caches() -> None:
    probed: list[str] = []

    async def probe(host: str) -> bool:
        probed.append(host)
        return host == "ext-strm-rukrd01seaix-02.strm.yandex.net"

    router = CdnRouter(probe=probe)
    signed = (
        "https://ext-strm-rukrd01seaix-03.strm.yandex.net/music-v2/raw/"
        "ysign1=abc,lid=1638/0/track/320.mp3?x=1"
    )
    rewritten = await router.rewrite(signed)
    assert rewritten.startswith(
        "https://ext-strm-rukrd01seaix-02.strm.yandex.net/music-v2/raw/"
    )
    assert rewritten.endswith("?x=1")

    first_calls = len(probed)
    assert first_calls >= 2
    again = await router.rewrite(signed)
    assert again == rewritten
    assert len(probed) == first_calls, "the chosen edge is cached"


async def test_rewrite_keeps_original_when_nothing_is_reachable() -> None:
    async def probe(_host: str) -> bool:
        return False

    router = CdnRouter(probe=probe)
    url = "https://ext-strm-rukrd01seaix-03.strm.yandex.net/a/b"
    assert await router.rewrite(url) == url


async def test_rewrite_ignores_other_hosts() -> None:
    async def probe(_host: str) -> bool:  # pragma: no cover - must not run
        return True

    async def no_redirect(_url: str) -> None:
        return None

    router = CdnRouter(probe=probe, redirect_resolver=no_redirect)
    url = "https://api.music.yandex.net/get-mp3/abc"
    assert await router.rewrite(url) == url
    assert await router.rewrite("https://api.music.yandex.net/other") == "https://api.music.yandex.net/other"


async def test_rewrite_resolves_get_mp3_redirect_then_picks_edge() -> None:
    edge = "https://ext-strm-rukrd01seaix-03.strm.yandex.net/music-v2/raw/x/320.mp3"

    async def redirect(_url: str) -> str:
        return edge

    async def probe(host: str) -> bool:
        return host == "ext-strm-rukrd01seaix-02.strm.yandex.net"

    router = CdnRouter(probe=probe, redirect_resolver=redirect)
    rewritten = await router.rewrite(
        "https://api.music.yandex.net/get-mp3/abc/def"
    )
    assert rewritten.startswith(
        "https://ext-strm-rukrd01seaix-02.strm.yandex.net/music-v2/raw/"
    )


async def test_probe_exception_is_treated_as_unreachable() -> None:
    async def probe(host: str) -> bool:
        if host.endswith("-03.strm.yandex.net"):
            raise OSError("dns failure")
        return host.endswith("-02.strm.yandex.net")

    router = CdnRouter(probe=probe)
    rewritten = await router.rewrite(
        "https://ext-strm-rukrd01seaix-03.strm.yandex.net/a"
    )
    assert "seaix-02" in rewritten


def test_real_probe_is_async_safe() -> None:
    # Smoke the default probe against a host that resolves locally.
    assert asyncio.run(_probe_localhost())


async def _probe_localhost() -> bool:
    import socket

    from app.integrations.yandex_music.cdn import tcp_probe

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        return await tcp_probe("127.0.0.1", port=port, timeout_s=1.0)
