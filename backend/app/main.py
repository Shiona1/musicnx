"""MusicNX FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.routes import (
    artwork,
    auth,
    bluetooth,
    downloads,
    history,
    library,
    lyrics,
    network,
    player,
    queue,
    stream,
    sync,
    system,
    system_backups,
    yandex,
    yandex_auth,
    zones,
)
from app.api.routes import (
    settings as settings_routes,
)
from app.api.websocket import router as websocket_router
from app.config import Settings
from app.context import STATIC_DIR, build_context
from app.errors import AppError
from app.logging_setup import get_logger
from app.security import hostname_from_authority, is_allowed_hostname, is_allowed_origin

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    context = await build_context(settings)
    app.state.ctx = context
    await context.start()
    logger.info("MusicNX %s started in %s mode", __version__, settings.mode)
    try:
        yield
    finally:
        logger.info("MusicNX stopping")
        await context.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="MusicNX",
        version=__version__,
        description="Headless media server: local library, player, queue and zones.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings or Settings()
    app.state.ctx = None

    if app.state.settings.security.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app.state.settings.security.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        security = app.state.settings.security
        if security.enforce_host:
            host = hostname_from_authority(request.headers.get("host"))
            if not is_allowed_hostname(host, security):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "host_forbidden",
                            "message": "Host header is not allowed",
                            "details": {"host": host},
                        }
                    },
                )
        origin = request.headers.get("origin")
        if origin is not None and not is_allowed_origin(origin, security):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "origin_forbidden",
                        "message": "Origin is not allowed",
                        "details": {"origin": origin},
                    }
                },
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response

    api_prefix = "/api/v1"
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)
    app.include_router(player.router, prefix=api_prefix)
    app.include_router(queue.router, prefix=api_prefix)
    app.include_router(library.router, prefix=api_prefix)
    app.include_router(history.router, prefix=api_prefix)
    app.include_router(artwork.router, prefix=api_prefix)
    app.include_router(yandex_auth.router, prefix=api_prefix)
    app.include_router(yandex.router, prefix=api_prefix)
    app.include_router(stream.router, prefix=api_prefix)
    app.include_router(downloads.router, prefix=api_prefix)
    app.include_router(lyrics.router, prefix=api_prefix)
    app.include_router(sync.router, prefix=api_prefix)
    app.include_router(zones.router, prefix=api_prefix)
    app.include_router(bluetooth.router, prefix=api_prefix)
    app.include_router(network.router, prefix=api_prefix)
    app.include_router(system_backups.router, prefix=api_prefix)
    app.include_router(settings_routes.router, prefix=api_prefix)
    app.include_router(websocket_router, prefix=api_prefix)

    _register_error_handlers(app)
    _register_static(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Never echo submitted values: a rejected token/PIN would otherwise be
        # returned in `input` (AGENTS §8).
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "ctx", "url"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": {"errors": jsonable_encoder(errors)},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        if exc.status_code == 405:
            code = "method_not_allowed"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": str(exc.detail),
                    "details": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal server error",
                    "details": {},
                }
            },
        )


def _register_static(app: FastAPI) -> None:
    index_file = STATIC_DIR / "index.html"
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    def index_response() -> FileResponse:
        # Never cache the shell: a new deployment must reach the browser.
        return FileResponse(
            index_file,
            headers={
                "Cache-Control": "no-store, max-age=0, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        if index_file.is_file():
            return index_response()
        return JSONResponse(
            {
                "name": "MusicNX",
                "version": __version__,
                "message": "Frontend is not built. Run `make build` or use the Vite dev server.",
                "api": "/api/v1/system/health",
                "docs": "/api/docs",
            }
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Any:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (STATIC_DIR / full_path).resolve()
        static_root = STATIC_DIR.resolve()
        if candidate.is_file() and candidate.is_relative_to(static_root):
            return FileResponse(candidate)
        if index_file.is_file():
            return index_response()
        raise HTTPException(status_code=404, detail="Not found")


def _static_dir() -> Path:
    return STATIC_DIR


app = create_app()
