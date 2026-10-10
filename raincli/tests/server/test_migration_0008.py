"""Migration 0008 (protocol §18.1 item 3) on a throwaway PostgreSQL database: mixed-team conversations are split
per team with nothing deleted and no unseen message hidden, and the downgrade restores the 0007 schema."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from test_migration_0006 import scratch_db  # noqa: F401 - fixture


def _user(conn, name):
    return conn.execute(text("INSERT INTO users (id, email, display_name, password_hash, is_active) "
                             "VALUES (gen_random_uuid(), :e, :n, 'x', true) RETURNING id"),
                        {"e": f"{name}@example.test", "n": name}).scalar()


def _team(conn, slug):
    return conn.execute(text("INSERT INTO teams (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                        {"s": slug}).scalar()


def _pair(conn, team, x, y, *, archived_through=None, created="2026-01-01"):
    a, b = sorted((x, y), key=lambda u: f"p:{u}")
    conv = conn.execute(text("""
        INSERT INTO conversations (id, team_id, a_user_id, b_user_id, a_key, b_key, is_default, created_at)
        VALUES (gen_random_uuid(), :t, :a, :b, :ak, :bk, true, :created) RETURNING id"""),
        {"t": team, "a": a, "b": b, "ak": f"p:{a}", "bk": f"p:{b}", "created": created}).scalar()
    if archived_through is not None:
        conn.execute(text("UPDATE conversations SET archived_at = now(), archived_by_key = a_key, "
                          "archived_through_seq = :s WHERE id = :c"), {"s": archived_through, "c": conv})
    return conv


def _message(conn, team, conv, sender, recipient, body):
    return conn.execute(text("""
        INSERT INTO messages (id, team_id, conversation_id, sender_user_id, recipient_user_id, body, delivery_state)
        VALUES (gen_random_uuid(), :t, :c, :s, :r, :body, 'stored') RETURNING id, seq"""),
        {"t": team, "c": conv, "s": sender, "r": recipient, "body": body}).one()


def _seed(conn):
    """Three person pairs, each with a default conversation in team A holding team-B messages as well."""
    alice, bob, carol, dave = (_user(conn, n) for n in ("alice", "bob", "carol", "dave"))
    team_a, team_b = _team(conn, "team-a"), _team(conn, "team-b")
    plain = _pair(conn, team_a, alice, bob)
    a1 = _message(conn, team_a, plain, alice, bob, "a1")
    b1 = _message(conn, team_b, plain, alice, bob, "b1")
    b2 = _message(conn, team_b, plain, bob, alice, "b2")
    # Archived through everything: the moved messages were seen, so team B's thread starts archived too.
    seen = _pair(conn, team_a, alice, carol, archived_through=0)
    _message(conn, team_a, seen, alice, carol, "seen a")
    s1 = _message(conn, team_b, seen, carol, alice, "seen b")
    conn.execute(text("UPDATE conversations SET archived_through_seq = :s WHERE id = :c"), {"s": s1.seq, "c": seen})
    # Archived before a team-B message arrived: that message was never seen, so team B's thread is unarchived.
    unseen_pair = _pair(conn, team_a, alice, dave)
    u1 = _message(conn, team_a, unseen_pair, alice, dave, "seen")
    conn.execute(text("UPDATE conversations SET archived_at = now(), archived_by_key = a_key, "
                      "archived_through_seq = :s WHERE id = :c"), {"s": u1.seq, "c": unseen_pair})
    unseen = _message(conn, team_b, unseen_pair, dave, alice, "not seen yet")
    return {"teams": (team_a, team_b), "plain": plain, "seen": seen, "unseen_pair": unseen_pair,
            "a1": a1.id, "b": (b1.id, b2.id), "s1": s1, "unseen": unseen}


def _conv_of(conn, message_id):
    return conn.execute(text("SELECT conversation_id, moved_from_conversation_id FROM messages WHERE id = :m"),
                        {"m": message_id}).one()


def test_0008_splits_mixed_conversations_per_team_and_downgrades(scratch_db):
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0007")
    with db.begin() as conn:
        seed = _seed(conn)
        before = conn.execute(text("SELECT count(*) FROM messages")).scalar()
    command.upgrade(cfg, "0008")
    team_a, team_b = seed["teams"]
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == before  # nothing deleted
        assert conn.execute(text("SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                                 "WHERE m.team_id <> c.team_id")).scalar() == 0
        # Team A's message stays; team B's move together into team B's default thread for the same pair.
        assert _conv_of(conn, seed["a1"]) == (seed["plain"], None)
        (target, source), (target2, source2) = (_conv_of(conn, m) for m in seed["b"])
        assert target == target2 != seed["plain"] and source == source2 == seed["plain"]
        new = conn.execute(text("SELECT * FROM conversations WHERE id = :c"), {"c": target}).mappings().one()
        old = conn.execute(text("SELECT * FROM conversations WHERE id = :c"), {"c": seed["plain"]}).mappings().one()
        assert new["team_id"] == team_b and new["is_default"] and new["archived_at"] is None
        assert (new["a_key"], new["b_key"], new["a_user_id"], new["b_user_id"]) == \
            (old["a_key"], old["b_key"], old["a_user_id"], old["b_user_id"])
        # Archive state: copied only when the archiver had seen every moved message.
        seen_target, _ = _conv_of(conn, seed["s1"].id)
        seen_new = conn.execute(text("SELECT * FROM conversations WHERE id = :c"), {"c": seen_target}).mappings().one()
        assert seen_new["archived_at"] is not None and seen_new["archived_through_seq"] == seed["s1"].seq
        unseen_target, _ = _conv_of(conn, seed["unseen"].id)
        unseen_new = conn.execute(text("SELECT * FROM conversations WHERE id = :c"),
                                  {"c": unseen_target}).mappings().one()
        assert unseen_new["archived_at"] is None and unseen_new["archived_through_seq"] is None
        assert {i["name"]: i["column_names"] for i in inspect(db).get_indexes("conversations")}[
            "uq_conversations_default_pair"] == ["team_id", "a_key", "b_key"]
        columns = {c["name"] for c in inspect(db).get_columns("users")}
        assert {"credential_epoch", "attachment_quota_bytes"} <= columns
    # A message can no longer sit in another team's conversation.
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text("UPDATE messages SET conversation_id = :c WHERE id = :m"),
                         {"c": seed["plain"], "m": seed["b"][0]})

    command.downgrade(cfg, "0007")
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == before
        assert _conv_of_0007(conn, seed["b"][0]) == target  # messages stay where they are
        defaults = conn.execute(text("SELECT id FROM conversations WHERE a_key = :a AND b_key = :b AND is_default"),
                                {"a": old["a_key"], "b": old["b_key"]}).scalars().all()
        assert defaults == [seed["plain"]]  # only the oldest default of the pair stays the default
    assert {i["name"]: i["column_names"] for i in inspect(db).get_indexes("conversations")}[
        "uq_conversations_default_pair"] == ["a_key", "b_key"]
    assert "credential_epoch" not in {c["name"] for c in inspect(db).get_columns("users")}
    assert "moved_from_conversation_id" not in {c["name"] for c in inspect(db).get_columns("messages")}
    assert not [fk for fk in inspect(db).get_foreign_keys("messages") if fk["name"] == "fk_messages_conversation_team"]
    command.upgrade(cfg, "head")  # and up again


def _conv_of_0007(conn, message_id):
    return conn.execute(text("SELECT conversation_id FROM messages WHERE id = :m"), {"m": message_id}).scalar()


def test_0008_on_data_with_nothing_mixed_changes_no_message(scratch_db):
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0007")
    with db.begin() as conn:
        alice, bob = _user(conn, "alice"), _user(conn, "bob")
        team = _team(conn, "only")
        conv = _pair(conn, team, alice, bob)
        ids = [_message(conn, team, conv, alice, bob, f"m{i}").id for i in range(3)]
    command.upgrade(cfg, "0008")
    with db.connect() as conn:
        assert [_conv_of(conn, m) for m in ids] == [(conv, None)] * 3
        assert conn.execute(text("SELECT count(*) FROM conversations")).scalar() == 1
