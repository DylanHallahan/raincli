"""Migration 0013 on a throwaway PostgreSQL database (protocol §23): grants, delegated person sessions and the acting
person on messages, the checks that keep them in shape, and an exact downgrade (with a live and a revoked grant, a
delegated session with its web session and handoff code, and a delegated send)."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from raincli_server.migrate import alembic_config, upgrade
from test_migration_0006 import scratch_db  # noqa: F401 - fixture
from test_migration_0010 import P, _msg
from test_migration_0011 import _schema
from test_migration_0012 import _at_0011

TABLES = ("teams", "agents", "conversations", "messages", "memberships", "users", "person_sessions", "web_sessions",
          "handoff_codes")


def _rows(conn):
    out = {}
    for t in TABLES:
        cols = [r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t ORDER BY ordinal_position"),
            {"t": t}) if not (t == "teams" and r[0] == "next_seq")]  # the counter moves with any new message
        out[t] = sorted(tuple(r) for r in conn.execute(text(f"SELECT {', '.join(cols)} FROM {t}")))
    return out


def _person_session(conn, user, machine, **delegated):
    sid = uuid.uuid4()
    cols = "".join(f", {k}" for k in delegated)
    vals = "".join(f", :{k}" for k in delegated)
    conn.execute(text(f"INSERT INTO person_sessions (id, user_id, machine_agent_id, token_hash, prefix, scopes{cols}) "
                      f"VALUES (:i, :u, :m, :h, 'rps_x', ARRAY['person:read','person:send']{vals})"),
                 {"i": sid, "u": user, "m": machine, "h": uuid.uuid4().hex * 2, **delegated})
    return sid


def _app_session(conn, user, ps):
    conn.execute(text("INSERT INTO web_sessions (id, user_id, token_hash, csrf_token, expires_at, app_mode, "
                      "person_session_id, app_install_hash) VALUES (gen_random_uuid(), :u, :h, 'c', "
                      "now() + interval '1 day', true, :p, :i)"),
                 {"u": user, "h": uuid.uuid4().hex * 2, "p": ps, "i": "a" * 64})
    conn.execute(text("INSERT INTO handoff_codes (id, code_hash, person_session_id, expires_at, install_hash) "
                      "VALUES (gen_random_uuid(), :h, :p, now() + interval '1 minute', :i)"),
                 {"h": uuid.uuid4().hex * 2, "p": ps, "i": "a" * 64})


def _grant(conn, team, grantee, account, revoked=False, all_members=False, toasts=True):
    gid = uuid.uuid4()
    conn.execute(text("INSERT INTO grants (id, team_id, grantee_user_id, account_user_id, created_by, revoked_at, "
                      f"all_members, toasts) VALUES (:i, :t, :g, :a, 'operator', {'now()' if revoked else 'NULL'}, "
                      ":all, :toasts)"),
                 {"i": gid, "t": team, "g": grantee, "a": account, "all": all_members, "toasts": toasts})
    return gid


def _at_0012(url, db):
    """The 0011 seed plus Maya's own app sign-in (a person session, its app-mode web session and a handoff code)."""
    seed = _at_0011(url, db)
    upgrade(url, "0012")
    with db.begin() as conn:
        seed["maya_own"] = _person_session(conn, seed["users"]["maya"], seed["machines"]["laptop"])
        _app_session(conn, seed["users"]["maya"], seed["maya_own"])
    return seed


def test_accounts_round_trip_and_the_downgrade_is_exact(scratch_db):  # noqa: F811
    url, db = scratch_db
    seed = _at_0012(url, db)
    nw, _ct = seed["teams"]
    maya, jon, priya = seed["users"]["maya"], seed["users"]["jon"], seed["users"]["priya"]
    with db.connect() as conn:
        schema, rows = _schema(conn), _rows(conn)

    upgrade(url, "0013")
    with db.begin() as conn:
        live = _grant(conn, nw, maya, jon)
        _grant(conn, nw, maya, priya, revoked=True)
        _grant(conn, nw, None, priya, all_members=True, toasts=False)  # A17: team-wide
        child = _person_session(conn, jon, seed["machines"]["laptop"], acting_user_id=maya, grant_id=live,
                                parent_session_id=seed["maya_own"])
        _app_session(conn, jon, child)
        sent = _msg(conn, nw, seed["jon"][0], P(jon), P(priya), "sent by Maya as Jon")
        conn.execute(text("UPDATE messages SET acting_user_id = :m, grant_id = :g WHERE id = :i"),
                     {"m": maya, "g": live, "i": sent.id})

    command.downgrade(alembic_config(url), "0012")
    with db.connect() as conn:
        assert _schema(conn) == schema
        after = _rows(conn)
    for table in TABLES:
        if table != "messages":
            assert after[table] == rows[table], table  # the delegated session, its web session and code are gone
    before_ids = {r[0] for r in rows["messages"]}
    assert [r for r in after["messages"] if r[0] in before_ids] == rows["messages"]  # every earlier row exactly
    assert len(after["messages"]) == len(rows["messages"]) + 1  # the send stays, without its acting person and grant
    upgrade(url, "0013")  # and back up again
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM grants")).scalar() == 0


def _refused(db, sql, params, match):
    with pytest.raises((IntegrityError, DBAPIError), match=match), db.begin() as conn:
        conn.execute(text(sql), params)


def test_the_checks_keep_grants_and_delegated_sessions_in_shape(scratch_db):  # noqa: F811
    url, db = scratch_db
    seed = _at_0012(url, db)
    upgrade(url, "0013")
    nw, _ct = seed["teams"]
    maya, jon, priya = seed["users"]["maya"], seed["users"]["jon"], seed["users"]["priya"]
    laptop = seed["machines"]["laptop"]
    with db.begin() as conn:
        live = _grant(conn, nw, maya, jon)
    # One live grant per pair; a revoked one may sit beside it; never yourself.
    _refused(db, "INSERT INTO grants (id, team_id, grantee_user_id, account_user_id, created_by, toasts) "
                 "VALUES (gen_random_uuid(), :t, :g, :a, 'operator', true)", {"t": nw, "g": maya, "a": jon},
             "uq_grants_live")
    with db.begin() as conn:
        _grant(conn, nw, maya, jon, revoked=True)
    _refused(db, "INSERT INTO grants (id, team_id, grantee_user_id, account_user_id, created_by, toasts) "
                 "VALUES (gen_random_uuid(), :t, :g, :g, 'operator', true)", {"t": nw, "g": maya}, "ck_grants_not_self")
    # A17: a team-wide grant has no grantee (and only it), one live per (team, account); toasts is always set (A19).
    for grantee, all_members in ((None, False), (maya, True)):
        _refused(db, "INSERT INTO grants (id, team_id, grantee_user_id, account_user_id, created_by, all_members, "
                     "toasts) VALUES (gen_random_uuid(), :t, :g, :a, 'operator', :all, true)",
                 {"t": nw, "g": grantee, "a": priya, "all": all_members}, "ck_grants_all_members")
    with db.begin() as conn:
        _grant(conn, nw, None, priya, all_members=True)
        _grant(conn, nw, None, priya, all_members=True, revoked=True)
    with pytest.raises((IntegrityError, DBAPIError), match="uq_grants_live_all_members"), db.begin() as conn:
        _grant(conn, nw, None, priya, all_members=True)
    _refused(db, "INSERT INTO grants (id, team_id, grantee_user_id, account_user_id, created_by) "
                 "VALUES (gen_random_uuid(), :t, :g, :a, 'operator')", {"t": nw, "g": maya, "a": priya}, "toasts")
    # A delegated session has all three of acting person, grant and parent, or none; never acting as itself.
    for cols in ({"acting_user_id": maya}, {"acting_user_id": maya, "grant_id": live},
                 {"grant_id": live, "parent_session_id": seed["maya_own"]}):
        with pytest.raises((IntegrityError, DBAPIError), match="ck_person_sessions_delegated"), db.begin() as conn:
            _person_session(conn, jon, laptop, **cols)
    with pytest.raises((IntegrityError, DBAPIError), match="ck_person_sessions_delegated"), db.begin() as conn:
        _person_session(conn, maya, laptop, acting_user_id=maya, grant_id=live, parent_session_id=seed["maya_own"])
    # The acting person is recorded on person sends only, always with the grant in effect (A19 D4).
    _refused(db, "UPDATE messages SET acting_user_id = :m, grant_id = :g WHERE sender_user_id IS NULL",
             {"m": maya, "g": live}, "ck_messages_acting_person")
    _refused(db, "UPDATE messages SET acting_user_id = :m WHERE sender_user_id IS NOT NULL", {"m": maya},
             "ck_messages_acting_grant")
    # M2: grants never key on memberships, so a member with a (revoked) grant can still be removed and re-added.
    with db.begin() as conn:
        conn.execute(text("UPDATE grants SET revoked_at = now() WHERE id = :i"), {"i": live})
        conn.execute(text("DELETE FROM memberships WHERE team_id = :t AND user_id = :u"), {"t": nw, "u": jon})
        conn.execute(text("INSERT INTO memberships (team_id, user_id, role) VALUES (:t, :u, 'member')"),
                     {"t": nw, "u": jon})
        assert conn.execute(text("SELECT count(*) FROM grants WHERE account_user_id = :u"), {"u": jon}).scalar() == 2
