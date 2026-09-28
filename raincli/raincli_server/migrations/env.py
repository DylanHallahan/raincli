"""Alembic environment: URL from RAINCLI_DATABASE_URL or config attribute 'url'."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from raincli_server.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url") or os.environ.get("RAINCLI_DATABASE_URL", "")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url:
        raise RuntimeError("set RAINCLI_DATABASE_URL")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
