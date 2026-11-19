"""FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from app.context import AppContext


def get_context(request: Request) -> AppContext:
    context: AppContext | None = getattr(request.app.state, "ctx", None)
    if context is None:  # pragma: no cover - lifespan must run first
        raise RuntimeError("application context is not initialised")
    return context


async def require_mutation_access(request: Request) -> None:
    """PIN confirmation was removed by the owner.

    Mutating endpoints remain protected by the Origin middleware, but no longer
    require a PIN/session. The request parameter is kept for dependency
    compatibility with existing routes.
    """

    del request
    return None
