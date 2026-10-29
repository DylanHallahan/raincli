"""Migration 0012 on a throwaway PostgreSQL database (protocol §22): group conversations, members, posts and copies,
the checks that keep them in shape, and an exact downgrade (with an acked copy, its events and a private reply)."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from raincli_server.migrate import alembic_config, upgrade
from test_migration_0006 import scratch_db  # noqa: F401 - fixture
from test_migration_0010 import P, _msg
from test_migration_0011 import _at_0010, _schema

TABLES = ("teams", "agents", "conversations", "messages", "person_inboxes", "memberships", "delivery_events",
          "users")


def _rows(conn):
    out = {}
    for t in TABLES:
        cols = [r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t ORDER BY ordinal_position"),
            {"t": t}) if not (t == "teams" and r[0] == "next_seq")]  # the counter moves with any new message
        out[t] = sorted(tuple(r) for r in conn.execute(text(f"SELECT {', '.join(cols)} FROM {t}")))
    return out


def _group(conn, team, name):
    gid = uuid.uuid4()
    conn.execute(text("INSERT INTO conversations (id, team_id, kind, name, a_key, b_key, is_default) "
                      "VALUES (:i, :t, 'group', :n, :a, :b, false)"),
                 {"i": gid, "t": team, "n": name, "a": f"group:{gid}", "b": f"group:{gid}:"})
    return gid


def _at_0011(url, db):
    seed = _at_0010(url, db)
    upgrade(url, "0011")
    return seed


def test_groups_round_trip_and_the_downgrade_is_exact(scratch_db):  # noqa: F811
    url, db = scratch_db
    seed = _at_0011(url, db)
    nw, _ct = seed["teams"]
    maya, jon = seed["users"]["maya"], seed["users"]["jon"]
    laptop, jon_laptop = seed["machines"]["laptop"], seed["machines"]["jon_laptop"]
    with db.connect() as conn:
        schema, rows = _schema(conn), _rows(conn)

    upgrade(url, "0012")
    with db.begin() as conn:
        gid = _group(conn, nw, "checkout")
        for person in (maya, jon):
            conn.execute(text("INSERT INTO group_members (conversation_id, user_id, team_id) VALUES (:g, :u, :t)"),
                         {"g": gid, "u": person, "t": nw})
        post = conn.execute(text(
            "INSERT INTO group_posts (id, team_id, conversation_id, author_agent_id, author_agent_name, "
            "author_user_id, body, mentions, recipients_m) VALUES (gen_random_uuid(), :t, :g, :a, 'laptop-agent', "
            ":u, 'hello group', ARRAY[:j]::uuid[], 1) RETURNING id, team_seq"),
            {"t": nw, "g": gid, "a": laptop, "u": maya, "j": jon}).one()
        top = conn.execute(text("SELECT max(team_seq) FROM messages WHERE team_id = :t"), {"t": nw}).scalar()
        assert post.team_seq > top  # the post's number comes from the team counter, after every message
        copy = conn.execute(text(
            "INSERT INTO messages (id, team_id, conversation_id, body, delivery_state, kind, route, post_id, "
            "sender_agent_id, sender_agent_name, recipient_agent_id, recipient_agent_name, acked_at) VALUES "
            "(gen_random_uuid(), :t, :g, 'hello group', 'received', 'message', 'group', :p, :a, 'laptop-agent', "
            ":r, 'api-agent', now()) RETURNING id, team_seq"),
            {"t": nw, "g": gid, "p": post.id, "a": laptop, "r": jon_laptop}).one()
        assert copy.team_seq == post.team_seq + 1  # one counter for posts and copies
        conn.execute(text("INSERT INTO delivery_events (message_id, state, reported_by) VALUES (:m, 'received', :r)"),
                     {"m": copy.id, "r": jon_laptop})
        # A private reply is an ordinary 1:1 message in their pair conversation, with no in_reply_to (§22).
        _msg(conn, nw, seed["jon"][0], P(jon), P(maya), "privately: done")

    command.downgrade(alembic_config(url), "0011")
    with db.connect() as conn:
        assert _schema(conn) == schema
        after = _rows(conn)
    for table in TABLES:
        if table != "messages":
            assert after[table] == rows[table], table
    before_ids = {r[0] for r in rows["messages"]}
    assert [r for r in after["messages"] if r[0] in before_ids] == rows["messages"]  # every earlier row exactly
    assert len(after["messages"]) == len(rows["messages"]) + 1  # the private reply stays; the copy is gone
    upgrade(url, "0012")  # and back up again
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM group_posts")).scalar() == 0


def _refused(db, sql, params, match):
    with pytest.raises((IntegrityError, DBAPIError), match=match), db.begin() as conn:
        conn.execute(text(sql), params)


def test_the_checks_keep_groups_in_shape(scratch_db):  # noqa: F811
    url, db = scratch_db
    seed = _at_0011(url, db)
    upgrade(url, "0012")
    nw, ct = seed["teams"]
    maya, jon, lee = seed["users"]["maya"], seed["users"]["jon"], seed["users"]["lee"]
    with db.begin() as conn:
        gid = _group(conn, nw, "checkout")
    # Names: unique per team without case (and normalized), 1-40 characters, no controls, breaks or bidi overrides.
    for name, match in (("Checkout", "uq_conversations_group_name"), ("", "group_name_text"),
                        ("x" * 41, "value too long|group_name_text"), ("a\nb", "group_name_text"),
                        ("evil‮gnp", "group_name_text"), (" padded", "group_name_text"),
                        ("café", "group_name_text")):
        with pytest.raises((IntegrityError, DBAPIError), match=match), db.begin() as conn:
            _group(conn, nw, name)
    with db.begin() as conn:
        _group(conn, ct, "checkout")  # another team may use the name
    # A group has a name, no sides, its own keys, and is never default.
    _refused(db, "INSERT INTO conversations (id, team_id, kind, a_key, b_key, is_default) "
                 "VALUES (gen_random_uuid(), :t, 'group', 'group:x', 'group:x:', false)", {"t": nw},
             "group_name|group_shape")
    _refused(db, "INSERT INTO conversations (id, team_id, kind, name, a_key, b_key, is_default) "
                 "VALUES (gen_random_uuid(), :t, 'group', 'other', 'group:x', 'group:x:', false)", {"t": nw},
             "group_shape")
    # Members must belong to the group's team (the membership key), and are never cascaded away silently.
    _refused(db, "INSERT INTO group_members (conversation_id, user_id, team_id) VALUES (:g, :u, :t)",
             {"g": gid, "u": lee, "t": nw}, "fk_group_members_membership")
    with db.begin() as conn:
        conn.execute(text("INSERT INTO group_members (conversation_id, user_id, team_id) VALUES (:g, :u, :t)"),
                     {"g": gid, "u": jon, "t": nw})
    _refused(db, "DELETE FROM memberships WHERE team_id = :t AND user_id = :u", {"t": nw, "u": jon},
             "fk_group_members_membership")
    # A copy goes to a machine, always with its post; only copies are ever withdrawn.
    _refused(db, "INSERT INTO messages (id, team_id, conversation_id, body, delivery_state, kind, route, "
                 "sender_user_id, recipient_user_id) VALUES (gen_random_uuid(), :t, :g, 'x', 'stored', 'message', "
                 "'group', :u, :r)", {"t": nw, "g": gid, "u": maya, "r": jon}, "ck_messages_group")
    _refused(db, "UPDATE messages SET withdrawn_at = now()", {}, "ck_messages_withdrawn_group")
