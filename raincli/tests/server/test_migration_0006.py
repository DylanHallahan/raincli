"""Migration 0006 (protocol §16.2, §16.6, §16.12 C7) on a throwaway PostgreSQL database."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError

ADMIN_URL = os.environ.get("RAINCLI_TEST_DATABASE_URL", "")


@pytest.fixture
def scratch_db():
    if not ADMIN_URL:
        pytest.skip("RAINCLI_TEST_DATABASE_URL not set")
    psycopg = "postgresql+psycopg://" + ADMIN_URL.split("://", 1)[1]
    name = f"raincli_mig6_{uuid.uuid4().hex[:12]}"
    server = create_engine(psycopg, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = psycopg.rsplit("/", 1)[0] + "/" + name
    db = create_engine(url)
    try:
        yield url, db
    finally:
        db.dispose()
        with server.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()


def _seed_v05(conn):
    user = conn.execute(text("INSERT INTO users (id, email, display_name, password_hash, is_active) "
                             "VALUES (gen_random_uuid(), 'm@example.test', 'M', 'x', true) RETURNING id")).scalar()
    team = conn.execute(text("INSERT INTO teams (id, slug, name) VALUES (gen_random_uuid(), 'mt', 'MT') "
                             "RETURNING id")).scalar()
    ids = [conn.execute(text("INSERT INTO agents (id, team_id, owner_user_id, handle, display_name) "
                             "VALUES (gen_random_uuid(), :t, :u, :h, :h) RETURNING id"),
                        {"t": team, "u": user, "h": h}).scalar() for h in ("one", "two")]
    a, b = sorted(ids)
    conv = conn.execute(text("INSERT INTO conversations (id, team_id, agent_a_id, agent_b_id, is_default) "
                             "VALUES (gen_random_uuid(), :t, :a, :b, true) RETURNING id"),
                        {"t": team, "a": a, "b": b}).scalar()
    conn.execute(text("INSERT INTO messages (id, team_id, conversation_id, sender_agent_id, recipient_agent_id, body, "
                      "delivery_state) VALUES (gen_random_uuid(), :t, :c, :a, :b, 'hello', 'stored')"),
                 {"t": team, "c": conv, "a": a, "b": b})
    return user, team, a, b, conv


def test_0006_backfills_machine_pairs_and_enforces_endpoints(scratch_db):
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0005")
    with db.begin() as conn:
        user, team, a, b, conv = _seed_v05(conn)
    command.upgrade(cfg, "head")
    with db.connect() as conn:
        keys = conn.execute(text("SELECT a_key, b_key FROM conversations WHERE id = :c"), {"c": conv}).one()
        assert keys == (f"m:{a}", f"m:{b}")
        kind = conn.execute(text("SELECT kind FROM messages")).scalar()
        assert kind == "message"
        routing = conn.execute(text("SELECT DISTINCT routing FROM agents")).scalars().all()
        assert routing == ["all"]

    def insert_message(**cols):
        values = {"id": uuid.uuid4(), "team_id": team, "conversation_id": conv, "body": "x", "delivery_state": "stored",
                  **cols}
        names = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        with db.begin() as conn:
            conn.execute(text(f"INSERT INTO messages ({names}) VALUES ({params})"), values)

    # C7: a machine may send to an agent on itself, never to its own machine endpoint.
    insert_message(sender_agent_id=a, recipient_agent_id=a, recipient_agent_name="reviewer")
    for bad in (dict(sender_agent_id=a, recipient_agent_id=a),
                dict(sender_agent_id=a, sender_agent_name="r", recipient_agent_id=a, recipient_agent_name="r"),
                dict(sender_user_id=user, recipient_user_id=user),
                dict(sender_agent_id=a, sender_user_id=user, recipient_agent_id=b),  # two senders
                dict(recipient_agent_id=b),  # no sender
                dict(sender_agent_id=a, recipient_user_id=user, recipient_agent_name="x"),  # name without machine
                dict(sender_agent_id=a, recipient_agent_id=b, kind="spam")):
        with pytest.raises(IntegrityError):
            insert_message(**bad)
    insert_message(sender_user_id=user, recipient_agent_id=b, recipient_agent_name="reviewer")

    # Directory: reachability on any agent, the inbox deliverable, ambiguous only when listed.
    now = "now()"
    def machine_agent(key, **cols):
        values = {"agent_id": a, "key": key, "name": "n", "type": "claude", "status": "idle", "source": "herdr", **cols}
        names = ", ".join(list(values) + ["seen_at"])
        params = ", ".join([f":{k}" for k in values] + [now])
        with db.begin() as conn:
            conn.execute(text(f"INSERT INTO machine_agents ({names}) VALUES ({params})"), values)

    machine_agent("k" * 32, reachability="listed", ambiguous=True)
    machine_agent("l" * 32, reachability="next-turn")
    for bad in (dict(role="inbox"), dict(role="inbox", reachability="listed"), dict(ambiguous=True),
                dict(reachability="nope")):
        with pytest.raises(IntegrityError):
            machine_agent("z" * 32, **bad)

    # Downgrading would lose person and agent endpoints, so it refuses.
    with pytest.raises((InternalError, ProgrammingError)):
        command.downgrade(cfg, "0005")


def test_0006_round_trip_without_new_endpoints(scratch_db):
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0005")
    with db.begin() as conn:
        _seed_v05(conn)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005")
    columns = {c["name"] for c in inspect(db).get_columns("messages")}
    assert not {"kind", "recipient_user_id", "sender_user_id", "recipient_agent_name"} & columns
    assert "person_sessions" not in inspect(db).get_table_names()
    command.upgrade(cfg, "head")


def test_0006_downgrade_deletes_app_mode_sessions_first(scratch_db):
    """§16.14 S5: an app-mode session must never survive as a plain, full web session."""
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0005")
    with db.begin() as conn:
        user, team, a, b, conv = _seed_v05(conn)
    command.upgrade(cfg, "head")
    with db.begin() as conn:
        ps = conn.execute(text("INSERT INTO person_sessions (id, user_id, machine_agent_id, token_hash, prefix, scopes) "
                               "VALUES (gen_random_uuid(), :u, :a, :h, 'rps_x', ARRAY['person:read']) RETURNING id"),
                          {"u": user, "a": a, "h": "p" * 64}).scalar()
        for token, app_mode in (("w" * 64, False), ("x" * 64, True)):
            conn.execute(text("INSERT INTO web_sessions (id, user_id, token_hash, csrf_token, expires_at, app_mode, "
                              "person_session_id, app_install_hash) VALUES (gen_random_uuid(), :u, :t, 'c', "
                              "now() + interval '1 day', :m, :ps, :ih)"),
                         {"u": user, "t": token, "m": app_mode, "ps": ps if app_mode else None,
                          "ih": "i" * 64 if app_mode else None})
        # An app-mode row needs its install binding, and a plain row may not have one (§16.14 S3).
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(text("INSERT INTO web_sessions (id, user_id, token_hash, csrf_token, expires_at, app_mode, "
                                  "person_session_id) VALUES (gen_random_uuid(), :u, 'y', 'c', now(), true, :ps)"),
                             {"u": user, "ps": ps})
    command.downgrade(cfg, "0005")
    with db.connect() as conn:
        assert conn.execute(text("SELECT token_hash FROM web_sessions")).scalars().all() == ["w" * 64]
    assert "app_install_hash" not in {c["name"] for c in inspect(db).get_columns("web_sessions")}
