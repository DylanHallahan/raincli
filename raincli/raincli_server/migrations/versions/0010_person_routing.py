"""Person routing (protocol §21): the inbox setting, person-pair conversations and redelivery.

Run with the service stopped; Alembic runs it in one transaction.

- person_inboxes: per person and team, the (machine, agent name) that receives their teammates' messages;
- conversations.kind: ``pair`` (two people), ``self`` (a person's own agents writing to them) or ``own_agents``
  (a person's machine-to-machine history, private); ``merged_into`` and ``premerge_is_default`` for the merge;
- messages.redeliver (re-targeted, returned below the poll cursor until acked), messages.route (how the
  recipient was chosen), messages.person_seen_at (the recipient person's own read state; acked_at is the machine's
  pickup) and messages.premerge_conversation_id (for the downgrade).

The merge. Every endpoint maps to its person (a machine, or an agent on it, to the machine's owner). Per team:
- two people: their conversations with any messages become one ``pair`` conversation between them (the existing
  default person-to-person row when there is one, which then counts as a source, otherwise a new one);
- a person and their own machine (escalations, their agents writing to them): one ``self`` conversation;
- two machines of one person: one ``own_agents`` conversation, always archived.
Messages move and keep their endpoints (premerge_conversation_id records where they were); every source row
stays, with ``merged_into`` set, its default mark cleared and recorded in ``premerge_is_default``. A pair or self
conversation is archived only if every source was archived through its newest message; then it is archived
through the newest of the sources' ``archived_through_seq``, by the most recent archiver mapped to their person.
A reused row's own archive state is kept in ``premerge_archive`` for the downgrade, which also marks the rows this
migration created. Unread state is per message (acked_at) and does not change. Conversations without messages
are left alone. Nothing is deleted.

The downgrade moves every merged message back and restores the sources' default marks and the reused rows'
archive state. A message sent after the upgrade whose endpoints are not its conversation's two keys (in a self,
own_agents or person-pair row) moves to the default conversation of its own endpoints, created when needed. Then
the self and own_agents rows and the created rows that are empty are deleted; a created person-pair row that keeps
later messages stays as an ordinary person-to-person conversation. Re-targets made after the upgrade are not
undone.
"""
import json
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

OWN_SHAPE = ("(kind = 'pair') = (b_key NOT LIKE 'self:%') AND (kind = 'pair' OR ("
             "agent_a_id IS NULL AND agent_b_id IS NULL AND a_user_id = b_user_id "
             "AND a_key = 'p:' || a_user_id::text AND b_key = 'self:' || a_user_id::text))")


def upgrade():
    op.create_table(
        "person_inboxes",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("set_via", sa.String(8), nullable=False),
        sa.CheckConstraint("set_via IN ('app','web','cli','api')", name="ck_person_inboxes_set_via"),
        sa.CheckConstraint("char_length(agent_name) BETWEEN 1 AND 64 AND agent_name NOT LIKE '-%'",
                           name="ck_person_inboxes_agent_name"),
    )
    op.create_index("ix_person_inboxes_agent", "person_inboxes", ["agent_id"])

    op.add_column("conversations", sa.Column("kind", sa.String(16), nullable=False, server_default="pair"))
    op.add_column("conversations", sa.Column("merged_into", UUID(as_uuid=True), nullable=True))
    op.add_column("conversations", sa.Column("premerge_is_default", sa.Boolean(), nullable=True))
    op.add_column("conversations", sa.Column("premerge_archive", JSONB(), nullable=True))
    op.create_foreign_key("fk_conversations_merged_into", "conversations", "conversations", ["merged_into"], ["id"])
    op.create_check_constraint("ck_conversations_kind", "conversations", "kind IN ('pair','self','own_agents')")
    op.create_check_constraint("ck_conversations_own_shape", "conversations", OWN_SHAPE)
    for kind in ("self", "own_agents"):
        op.create_index(f"uq_conversations_{kind}", "conversations", ["team_id", "a_user_id"], unique=True,
                        postgresql_where=sa.text(f"kind = '{kind}'"))
    op.create_index("ix_conversations_merged_into", "conversations", ["merged_into"],
                    postgresql_where=sa.text("merged_into IS NOT NULL"))

    op.add_column("messages", sa.Column("premerge_conversation_id", UUID(as_uuid=True), nullable=True))
    op.add_column("messages", sa.Column("redeliver", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("messages", sa.Column("route", sa.String(8), nullable=True))
    op.add_column("messages", sa.Column("person_seen_at", sa.DateTime(timezone=True), nullable=True))
    # A person's read state so far was acked_at on messages to them.
    op.execute("UPDATE messages SET person_seen_at = acked_at WHERE recipient_user_id IS NOT NULL")
    op.create_check_constraint("ck_messages_route", "messages", "route IN ('inbox','reply','person','self')")
    for column in ("agent", "user"):
        op.create_index(f"ix_messages_redeliver_{column}", "messages", [f"recipient_{column}_id", "seq"],
                        postgresql_where=sa.text("redeliver AND acked_at IS NULL"))
    _merge(op.get_bind())


# Each conversation with messages, each side as (person, is a machine).
SOURCES = """
SELECT c.id, c.team_id, c.a_key, c.b_key, c.is_default, c.created_at,
       c.archived_at, c.archived_by_key, c.archived_through_seq,
       coalesce(c.a_user_id, aa.owner_user_id) AS a_person, c.agent_a_id IS NOT NULL AS a_machine,
       coalesce(c.b_user_id, ab.owner_user_id) AS b_person, c.agent_b_id IS NOT NULL AS b_machine,
       (SELECT max(m.seq) FROM messages m WHERE m.conversation_id = c.id) AS max_seq
FROM conversations c
LEFT JOIN agents aa ON aa.id = c.agent_a_id
LEFT JOIN agents ab ON ab.id = c.agent_b_id
WHERE EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
ORDER BY c.created_at, c.id
"""


def _person_key(conn, key):
    """An endpoint key as its person's key (m:<machine> and a:<machine>:<name> map to the machine's owner)."""
    if key is None or key.startswith("p:"):
        return key
    owner = conn.execute(sa.text("SELECT owner_user_id FROM agents WHERE id = :id"),
                         {"id": key.split(":")[1]}).scalar()
    return f"p:{owner}" if owner is not None else key


def _archive(conn, sources, *, always, now):
    """``(archived_at, archived_by_key, archived_through_seq)`` of a merged conversation: archived only if every
    source with messages is archived through its newest one (an own_agents conversation always is)."""
    with_messages = [s for s in sources if s["max_seq"] is not None]
    covered = [s for s in with_messages
               if s["archived_at"] is not None and s["max_seq"] <= s["archived_through_seq"]]
    if covered and len(covered) == len(with_messages):
        last = max(covered, key=lambda s: (s["archived_at"], str(s["id"])))
        return (last["archived_at"], _person_key(conn, last["archived_by_key"]),
                max(s["archived_through_seq"] for s in covered))
    if always:
        return now, None, max(s["max_seq"] for s in with_messages)
    return None, None, None


def _iso(value):
    return value.isoformat() if value is not None else None


def _merge(conn):
    now = conn.execute(sa.text("SELECT now()")).scalar()
    groups: dict = {}
    for row in conn.execute(sa.text(SOURCES)).mappings().all():
        a, b = str(row["a_person"]), str(row["b_person"])
        if a != b:
            kind, people = "pair", tuple(sorted((a, b)))
        elif row["a_machine"] and row["b_machine"]:
            kind, people = "own_agents", (a, a)
        else:
            kind, people = "self", (a, a)
        groups.setdefault((row["team_id"], kind, people), []).append(dict(row))

    for (team, kind, (lo, hi)), sources in groups.items():
        a_key, b_key = (f"p:{lo}", f"p:{hi}") if kind == "pair" else (f"p:{lo}", f"self:{lo}")
        target = None
        if kind == "pair":
            target = next((s for s in sources if s["is_default"] and (s["a_key"], s["b_key"]) == (a_key, b_key)), None)
            if target is None:  # a default person-to-person row without messages is reused (and counts) as well
                found = conn.execute(sa.text(
                    "SELECT id, team_id, a_key, b_key, is_default, created_at, archived_at, archived_by_key, "
                    "archived_through_seq, NULL::bigint AS max_seq FROM conversations "
                    "WHERE team_id = :t AND a_key = :a AND b_key = :b AND is_default"),
                    {"t": team, "a": a_key, "b": b_key}).mappings().first()
                if found is not None:
                    target = dict(found)
        archived_at, archived_by, through = _archive(conn, sources, always=kind == "own_agents", now=now)
        if archived_at is not None and archived_by is None:
            archived_by = a_key
        if target is None:
            target_id = conn.execute(sa.text("""
                INSERT INTO conversations (id, team_id, a_user_id, b_user_id, a_key, b_key, is_default, created_at,
                    kind, archived_at, archived_by_key, archived_through_seq, premerge_archive)
                VALUES (:id, :team, :lo, :hi, :a, :b, :default, :created, :kind, :at, :by, :through,
                    CAST(:state AS jsonb))
                RETURNING id"""), {
                "id": uuid.uuid4(), "team": team, "lo": lo, "hi": hi, "a": a_key, "b": b_key,
                "default": kind != "own_agents", "created": min(s["created_at"] for s in sources), "kind": kind,
                "at": archived_at, "by": archived_by, "through": through,
                "state": json.dumps({"created": True})}).scalar()
        else:
            target_id = target["id"]
            conn.execute(sa.text("""
                UPDATE conversations SET archived_at = :at, archived_by_key = :by, archived_through_seq = :through,
                    premerge_archive = CAST(:state AS jsonb)
                WHERE id = :id"""), {
                "id": target_id, "at": archived_at, "by": archived_by, "through": through,
                "state": json.dumps({"archived_at": _iso(target["archived_at"]),
                                     "archived_by_key": target["archived_by_key"],
                                     "archived_through_seq": target["archived_through_seq"]})})
        for source in sources:
            if source["id"] == target_id:
                continue
            conn.execute(sa.text(
                "UPDATE messages SET premerge_conversation_id = conversation_id, conversation_id = :target "
                "WHERE conversation_id = :source"), {"target": target_id, "source": source["id"]})
            conn.execute(sa.text(
                "UPDATE conversations SET merged_into = :target, premerge_is_default = is_default, "
                "is_default = false WHERE id = :source"), {"target": target_id, "source": source["id"]})


def _endpoint(agent_id, name, user_id):
    """A message endpoint as conversation columns, with its canonical key (as routing.Endpoint.key)."""
    if user_id is not None:
        return {"agent": None, "name": None, "user": user_id, "key": f"p:{user_id}"}
    return {"agent": agent_id, "name": name, "user": None, "key": f"a:{agent_id}:{name}" if name else f"m:{agent_id}"}


def _unmerge(conn):
    conn.execute(sa.text("UPDATE messages SET conversation_id = premerge_conversation_id "
                         "WHERE premerge_conversation_id IS NOT NULL"))
    conn.execute(sa.text("UPDATE conversations SET merged_into = NULL WHERE merged_into IS NOT NULL"))
    # Default marks back (cleared first, so the default-pair index never collides), and reused rows' archive state.
    conn.execute(sa.text("UPDATE conversations SET is_default = false WHERE premerge_is_default IS NOT NULL"))
    conn.execute(sa.text("UPDATE conversations SET is_default = premerge_is_default, premerge_is_default = NULL "
                         "WHERE premerge_is_default IS NOT NULL"))
    conn.execute(sa.text("""
        UPDATE conversations SET
            archived_at = (premerge_archive->>'archived_at')::timestamptz,
            archived_by_key = premerge_archive->>'archived_by_key',
            archived_through_seq = (premerge_archive->>'archived_through_seq')::bigint,
            premerge_archive = NULL
        WHERE premerge_archive IS NOT NULL AND NOT premerge_archive ? 'created'"""))
    # Messages sent after the upgrade whose endpoints are not their conversation's two keys: to the default
    # conversation of their own endpoints (created when needed).
    rows = conn.execute(sa.text("""
        SELECT m.id, m.team_id, m.created_at, c.a_key, c.b_key,
               m.sender_agent_id, m.sender_agent_name, m.sender_user_id,
               m.recipient_agent_id, m.recipient_agent_name, m.recipient_user_id
        FROM messages m JOIN conversations c ON c.id = m.conversation_id
        WHERE m.premerge_conversation_id IS NULL
          AND (c.kind <> 'pair' OR (c.a_key LIKE 'p:%' AND c.b_key LIKE 'p:%'))
        ORDER BY m.seq""")).mappings().all()
    for m in rows:
        a, b = sorted([_endpoint(m["sender_agent_id"], m["sender_agent_name"], m["sender_user_id"]),
                       _endpoint(m["recipient_agent_id"], m["recipient_agent_name"], m["recipient_user_id"])],
                      key=lambda e: e["key"])
        if (a["key"], b["key"]) == (m["a_key"], m["b_key"]):
            continue
        target = conn.execute(sa.text(
            "SELECT id FROM conversations WHERE team_id = :t AND a_key = :a AND b_key = :b AND is_default"),
            {"t": m["team_id"], "a": a["key"], "b": b["key"]}).scalar()
        if target is None:
            target = conn.execute(sa.text("""
                INSERT INTO conversations (id, team_id, agent_a_id, agent_b_id, a_agent_name, b_agent_name,
                    a_user_id, b_user_id, a_key, b_key, is_default, created_at)
                VALUES (:id, :t, :aa, :ab, :an, :bn, :au, :bu, :ak, :bk, true, :created) RETURNING id"""), {
                "id": uuid.uuid4(), "t": m["team_id"], "aa": a["agent"], "ab": b["agent"], "an": a["name"],
                "bn": b["name"], "au": a["user"], "bu": b["user"], "ak": a["key"], "bk": b["key"],
                "created": m["created_at"]}).scalar()
        conn.execute(sa.text("UPDATE messages SET conversation_id = :c WHERE id = :m"), {"c": target, "m": m["id"]})
    conn.execute(sa.text("""
        DELETE FROM conversations c
        WHERE (c.kind <> 'pair' OR c.premerge_archive ? 'created')
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)"""))
    conn.execute(sa.text("UPDATE conversations SET premerge_archive = NULL WHERE premerge_archive IS NOT NULL"))


def downgrade():
    _unmerge(op.get_bind())
    # Reads made after the upgrade become acked_at again (before 0010 a person's read was acked_at).
    op.execute("UPDATE messages SET acked_at = person_seen_at WHERE recipient_user_id IS NOT NULL "
               "AND acked_at IS NULL AND person_seen_at IS NOT NULL")
    for column in ("agent", "user"):
        op.drop_index(f"ix_messages_redeliver_{column}", table_name="messages")
    op.drop_constraint("ck_messages_route", "messages", type_="check")
    op.drop_column("messages", "person_seen_at")
    op.drop_column("messages", "route")
    op.drop_column("messages", "redeliver")
    op.drop_column("messages", "premerge_conversation_id")
    op.drop_index("ix_conversations_merged_into", table_name="conversations")
    for kind in ("self", "own_agents"):
        op.drop_index(f"uq_conversations_{kind}", table_name="conversations")
    op.drop_constraint("ck_conversations_own_shape", "conversations", type_="check")
    op.drop_constraint("ck_conversations_kind", "conversations", type_="check")
    op.drop_constraint("fk_conversations_merged_into", "conversations", type_="foreignkey")
    op.drop_column("conversations", "premerge_archive")
    op.drop_column("conversations", "premerge_is_default")
    op.drop_column("conversations", "merged_into")
    op.drop_column("conversations", "kind")
    op.drop_index("ix_person_inboxes_agent", table_name="person_inboxes")
    op.drop_table("person_inboxes")
