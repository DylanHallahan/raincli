"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from raincli_server import __version__
from raincli_server.config import Settings, load_settings
from raincli_server.db import make_engine, make_sessionmaker

log = logging.getLogger("raincli_server")


SWEEP_SECONDS = 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    """An hourly sweep of short-lived rows (§18.1 item 5), for as long as the app runs."""
    task = asyncio.create_task(_sweep_forever(app))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def sweep(app: FastAPI) -> None:
    from raincli_server import identity

    with app.state.sessionmaker() as session, session.begin():
        identity.purge_handoff_codes(session)


async def _sweep_forever(app: FastAPI) -> None:
    from fastapi.concurrency import run_in_threadpool

    while True:
        try:
            await run_in_threadpool(sweep, app)
        except Exception:  # noqa: BLE001 - a sweep that fails (the database restarting) runs again next hour
            log.warning("hourly sweep failed; retrying in an hour")
        await asyncio.sleep(SWEEP_SECONDS)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    app = FastAPI(
        title="RainCLI", version=__version__, root_path=settings.root_path,
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    from raincli_server.web.auth import LoginLimiter

    # One limiter for every password sign-in: the website and POST /api/v1/app/login (protocol §15.1).
    app.state.web_login_limiter = LoginLimiter()

    @app.get("/api/v1/health", include_in_schema=False)
    def health():
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"ok": False, "db": "unavailable"}, status_code=503)
        return {"ok": True, "db": "ok"}

    from raincli_server.api import register as register_api
    from raincli_server.web import register as register_web

    register_api(app)
    register_web(app)
    return app


def app_from_env() -> FastAPI:  # uvicorn --factory raincli_server.app:app_from_env
    return create_app()
