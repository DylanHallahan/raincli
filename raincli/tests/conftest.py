"""Real PostgreSQL fixtures.

Set RAINCLI_TEST_DATABASE_URL to an admin URL on a *test* server (never production),
for example the loopback container started by ``scripts/test-postgres.sh``.
Each pytest session creates a throwaway database, migrates it with Alembic and drops it.
Server tests are skipped (not faked) when no test database is configured.
"""

from __future__ import annotations

import os
import uuid

import pytest

TEST_ADMIN_URL = os.environ.get("RAINCLI_TEST_DATABASE_URL", "")


def _psycopg_url(url: str) -> str:
    return "postgresql+psycopg://" + url.split("://", 1)[1]


@pytest.fixture(scope="session")
def database_url():
    if not TEST_ADMIN_URL:
        pytest.skip("RAINCLI_TEST_DATABASE_URL not set; run scripts/test-postgres.sh")
    from sqlalchemy import create_engine, text

    from raincli_server.migrate import upgrade

    name = f"raincli_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_psycopg_url(TEST_ADMIN_URL), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _psycopg_url(TEST_ADMIN_URL.rsplit("/", 1)[0] + "/" + name)
    upgrade(url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url):
    from raincli_server.db import make_engine

    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(request):
    yield
    if "engine" not in request.fixturenames:
        return
    eng = request.getfixturevalue("engine")
    from sqlalchemy import text

    from raincli_server.models import Base

    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with eng.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def session(engine):
    from raincli_server.db import make_sessionmaker

    s = make_sessionmaker(engine)()
    yield s
    try:
        s.rollback()
        s.close()
    except Exception:  # connection may have been killed (e.g. a PostgreSQL restart test)
        s.invalidate()
        engine.dispose()


@pytest.fixture
def settings(database_url):
    from raincli_server.config import load_settings

    return load_settings({
        "RAINCLI_DATABASE_URL": database_url,
        "RAINCLI_SECRET_KEY": "test-secret-key-" + "x" * 32,
        "RAINCLI_PUBLIC_URL": "http://testserver",
        "RAINCLI_COOKIE_SECURE": "0",
        "RAINCLI_MAX_PENDING": "5",
        "RAINCLI_RATE_LIMIT_PER_MIN": "10000",
    })


@pytest.fixture
def app(settings, engine):
    from raincli_server.app import create_app

    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def world(session):
    """Two teams: acme (alice, bob agents) and globex (eve agent). Returns raw tokens."""
    from raincli_server import identity

    u_alice = identity.create_user(session, "alice@example.test", "Alice", "correct horse battery")
    u_bob = identity.create_user(session, "bob@example.test", "Bob", "correct horse battery")
    u_eve = identity.create_user(session, "eve@example.test", "Eve", "correct horse battery")
    acme = identity.create_team(session, "acme", "Acme", u_alice)
    identity.add_member(session, acme, u_bob)
    globex = identity.create_team(session, "globex", "Globex", u_eve)
    a_alice, t_alice = identity.register_agent(session, acme, u_alice, "alice-agent")
    a_bob, t_bob = identity.register_agent(session, acme, u_bob, "bob-agent")
    a_eve, t_eve = identity.register_agent(session, globex, u_eve, "eve-agent")
    session.commit()
    return {
        "users": {"alice": u_alice, "bob": u_bob, "eve": u_eve},
        "teams": {"acme": acme, "globex": globex},
        "agents": {"alice": a_alice, "bob": a_bob, "eve": a_eve},
        "tokens": {"alice": t_alice, "bob": t_bob, "eve": t_eve},
    }
