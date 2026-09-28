"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from raincli_server import __version__
from raincli_server.config import Settings, load_settings
from raincli_server.db import make_engine, make_sessionmaker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    app = FastAPI(
        title="RainCLI", version=__version__, root_path=settings.root_path,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

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
