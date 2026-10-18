"""FastAPI application factory. Read-only routes + guarded control routes + static dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import control, routes
from app.api.deps import ApiState
from app.config.settings import Settings
from app.database.engine import create_engine, init_schema
from app.database.repository import Repository

FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(
    settings: Settings, repo: Repository | None = None, *, serve_frontend: bool = True
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        r = repo
        engine = None
        if r is None:
            engine = create_engine(settings.config.system.database_url)
            await init_schema(engine)
            r = Repository(engine)
        app.state.api = ApiState(settings=settings, repo=r)
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="Quant Trader Dashboard API", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.include_router(routes.router)
    app.include_router(control.router)

    if serve_frontend and FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            target = FRONTEND_DIST / path
            if path and target.is_file():
                return FileResponse(target)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app
