"""Team scope (protocol §21.13): a per-team message sequence, and the database keeps every machine in its row's team.

Run with the service stopped; Alembic runs it in one transaction.

- ``messages.team_seq``: the number every reader sees as ``seq`` and uses as a cursor. It is allocated per team by a
  trigger from ``teams.next_seq``, whose row stays locked to the end of the sending transaction, so a team's
  numbers commit in increasing order and say nothing about other teams. Existing messages keep their number
  (``team_seq = seq``); every team, and every team made later, continues from the highest number issued so far
  plus one, so every cursor a client has stored stays below every new message. The global ``seq`` stays as it is,
  for the server's own use.
- ``agents.stale_cursor``: a machine's last cursor that was above anything its team had issued (one kept from a
  machine that was signed in to another team); polls with it are answered from the start.
- Composite foreign keys keep a message's, an inbox's and a conversation's machines in that row's team. The upgrade
  stops, naming the constraint, if any existing row would break one.

The downgrade drops all of it; the global ``seq`` was never changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# (constraint, table, columns): each machine column with its row's team, against agents(id, team_id).
TEAM_FKS = (
    ("fk_messages_sender_agent_team", "messages", ["sender_agent_id", "team_id"]),
    ("fk_messages_recipient_agent_team", "messages", ["recipient_agent_id", "team_id"]),
    ("fk_person_inboxes_agent_team", "person_inboxes", ["agent_id", "team_id"]),
    ("fk_conversations_agent_a_team", "conversations", ["agent_a_id", "team_id"]),
    ("fk_conversations_agent_b_team", "conversations", ["agent_b_id", "team_id"]),
)

# The indexes behind the cursors and orderings, as on the global seq.
TEAM_SEQ_INDEXES = (
    ("ix_messages_recipient_team_seq", ["recipient_agent_id", "team_seq"], None),
    ("ix_messages_recipient_pending_team_seq", ["recipient_agent_id", "team_seq"], "acked_at IS NULL"),
    ("ix_messages_recipient_user_team_seq", ["recipient_user_id", "team_seq"], None),
    ("ix_messages_conversation_team_seq", ["conversation_id", "team_seq"], None),
    ("ix_messages_redeliver_agent_team_seq", ["recipient_agent_id", "team_seq"], "redeliver AND acked_at IS NULL"),
    ("ix_messages_redeliver_user_team_seq", ["recipient_user_id", "team_seq"], "redeliver AND acked_at IS NULL"),
)

ALLOCATE = """
CREATE FUNCTION messages_allocate_team_seq() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE teams SET next_seq = next_seq + 1 WHERE id = NEW.team_id RETURNING next_seq - 1 INTO NEW.team_seq;
    IF NEW.team_seq IS NULL THEN
        RAISE EXCEPTION 'no team % for message %', NEW.team_id, NEW.id USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$
"""


def _violations(conn, table, columns):
    machine, team = columns
    return conn.execute(sa.text(
        f"SELECT count(*) FROM {table} t JOIN agents a ON a.id = t.{machine} WHERE a.team_id <> t.{team}")).scalar()


def upgrade():
    conn = op.get_bind()
    broken = [(name, n) for name, table, columns in TEAM_FKS if (n := _violations(conn, table, columns))]
    if broken:
        raise RuntimeError("migration 0011 stopped: rows whose machine is in another team: "
                           + ", ".join(f"{name} ({n})" for name, n in broken))

    # Above the highest number ever issued: the identity sequence's last value, which is above max(seq) when the
    # newest messages were deleted (a team deletion cascades to its messages) and a client may have stored it.
    start = conn.execute(sa.text(
        "SELECT greatest((SELECT coalesce(max(seq), 0) FROM messages), "
        "coalesce(pg_sequence_last_value(pg_get_serial_sequence('messages', 'seq')), 0)) + 1")).scalar()
    op.add_column("teams", sa.Column("next_seq", sa.BigInteger(), nullable=True))
    op.execute(sa.text("UPDATE teams SET next_seq = :start").bindparams(start=start))
    op.alter_column("teams", "next_seq", nullable=False, server_default=sa.text(str(int(start))))

    op.add_column("messages", sa.Column("team_seq", sa.BigInteger(), nullable=True))
    op.execute("UPDATE messages SET team_seq = seq")
    op.alter_column("messages", "team_seq", nullable=False)
    op.create_unique_constraint("uq_messages_team_seq", "messages", ["team_id", "team_seq"])
    for name, columns, where in TEAM_SEQ_INDEXES:
        op.create_index(name, "messages", columns, postgresql_where=sa.text(where) if where else None)
    op.execute(ALLOCATE)
    op.execute("CREATE TRIGGER trg_messages_team_seq BEFORE INSERT ON messages "
               "FOR EACH ROW EXECUTE FUNCTION messages_allocate_team_seq()")

    op.add_column("agents", sa.Column("stale_cursor", sa.BigInteger(), nullable=True))

    op.create_unique_constraint("uq_agents_id_team", "agents", ["id", "team_id"])
    for name, table, columns in TEAM_FKS:
        op.create_foreign_key(name, table, "agents", columns, ["id", "team_id"])


def downgrade():
    for name, table, _columns in reversed(TEAM_FKS):
        op.drop_constraint(name, table, type_="foreignkey")
    op.drop_constraint("uq_agents_id_team", "agents", type_="unique")
    op.drop_column("agents", "stale_cursor")
    op.execute("DROP TRIGGER trg_messages_team_seq ON messages")
    op.execute("DROP FUNCTION messages_allocate_team_seq()")
    for name, _columns, _where in reversed(TEAM_SEQ_INDEXES):
        op.drop_index(name, table_name="messages")
    op.drop_constraint("uq_messages_team_seq", "messages", type_="unique")
    op.drop_column("messages", "team_seq")
    op.drop_column("teams", "next_seq")
