"""Migration 0007 (protocol §17.2, §17.3 A1, A7) on a throwaway PostgreSQL database."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from test_migration_0006 import _seed_v05, scratch_db  # noqa: F401 - fixture


def test_0007_adds_the_archive_state_and_downgrades_to_exactly_0006(scratch_db):
    from alembic import command

    from raincli_server.migrate import alembic_config

    url, db = scratch_db
    cfg = alembic_config(url)
    command.upgrade(cfg, "0005")
    with db.begin() as conn:
        _seed_v05(conn)
    command.upgrade(cfg, "0006")
    before = {c["name"] for c in inspect(db).get_columns("conversations")}
    before_indexes = {i["name"] for i in inspect(db).get_indexes("conversations")}
    command.upgrade(cfg, "0007")
    columns = {c["name"] for c in inspect(db).get_columns("conversations")}
    assert columns - before == {"archived_at", "archived_by_key", "archived_through_seq"}
    indexes = {i["name"]: i for i in inspect(db).get_indexes("conversations")}
    for column in ("agent_a_id", "agent_b_id", "a_user_id", "b_user_id"):  # A7: the main lists stay index-served
        index = indexes[f"ix_conversations_{column}_active"]
        assert index["column_names"] == [column]
        assert "archived_at IS NULL" in index["dialect_options"]["postgresql_where"]
    with db.begin() as conn:
        (cid,) = conn.execute(text("SELECT id FROM conversations")).one()
        conn.execute(text("UPDATE conversations SET archived_at = now(), archived_by_key = 'p:x', "
                          "archived_through_seq = 1 WHERE id = :c"), {"c": cid})
    with pytest.raises(IntegrityError):  # all three together, or none
        with db.begin() as conn:
            conn.execute(text("UPDATE conversations SET archived_by_key = NULL WHERE id = :c"), {"c": cid})
    command.downgrade(cfg, "0006")
    assert {c["name"] for c in inspect(db).get_columns("conversations")} == before
    assert {i["name"] for i in inspect(db).get_indexes("conversations")} == before_indexes
    with db.connect() as conn:  # nothing else was dropped: the conversation and its message stay
        assert conn.execute(text("SELECT count(*) FROM conversations")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == 1
    command.upgrade(cfg, "head")
