"""Website and browser app. Owned by the web builder; see docs/raincli-protocol.md §6."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    """Attach web routes, templates, static files and browser-session handling to ``app``."""
    from raincli_server.web.routes import install

    install(app)
