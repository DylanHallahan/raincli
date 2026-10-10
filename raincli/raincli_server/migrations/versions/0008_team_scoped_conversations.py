"""Team-scoped conversations, credential epochs and attachment quota overrides (protocol §18.1).

Run with the service stopped; Alembic runs it in one transaction.

- conversations: the default thread of a pair is unique per team, ``(team_id, a_key, b_key) WHERE is_default``,
  and ``(id, team_id)`` is unique so that messages can reference it;
- messages: ``(conversation_id, team_id)`` references ``conversations(id, team_id)``, so a message always lives
  in a conversation of its own team. A message whose team differed from its conversation's is moved into its
  team's default conversation for the same pair (created when needed) and keeps where it came from in
  ``moved_from_conversation_id``, so a retried send naming the old conversation is still the same payload.
  A conversation created here copies the archive state of the one the messages came from when every moved
  message is at or below that one's ``archived_through_seq``; otherwise it starts unarchived. Nothing is
  deleted;
- users.credential_epoch: bumped whenever the user's credentials change, ending recognised devices;
- users.attachment_quota_bytes and teams.attachment_quota_bytes: per-user (daily) and per-team (30-day)
  overrides of the configured attachment quotas; NULL means the configured default.

The downgrade leaves every message where it is, keeps only the oldest default conversation of each pair as
the default, and restores the pair-only index.
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Every message whose team differs from its conversation's, grouped by (source conversation, message team).
MIXED = """
SELECT c.id AS source, m.team_id AS team, max(m.seq) AS max_seq, min(m.created_at) AS first_at
FROM messages m JOIN conversations c ON c.id = m.conversation_id
WHERE m.team_id <> c.team_id
GROUP BY c.id, m.team_id
"""


def upgrade():
    op.add_column("users", sa.Column("credential_epoch", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("attachment_quota_bytes", sa.BigInteger(), nullable=True))
    op.add_column("teams", sa.Column("attachment_quota_bytes", sa.BigInteger(), nullable=True))
    op.add_column("messages", sa.Column("moved_from_conversation_id", UUID(as_uuid=True), nullable=True))

    # The default pair becomes per team before any team's own default thread is created below.
    op.drop_index("uq_conversations_default_pair", table_name="conversations")
    op.create_index("uq_conversations_default_pair", "conversations", ["team_id", "a_key", "b_key"], unique=True,
                    postgresql_where=sa.text("is_default"))
    op.create_unique_constraint("uq_conversations_id_team", "conversations", ["id", "team_id"])

    conn = op.get_bind()
    for source, team, max_seq, first_at in conn.execute(sa.text(MIXED)).all():
        src = conn.execute(sa.text("SELECT * FROM conversations WHERE id = :id"), {"id": source}).mappings().one()
        target = conn.execute(sa.text(
            "SELECT id FROM conversations WHERE team_id = :team AND a_key = :a AND b_key = :b AND is_default"),
            {"team": team, "a": src["a_key"], "b": src["b_key"]}).scalar()
        if target is None:
            # The archive state carries over only when the archiver had seen every moved message.
            covered = src["archived_through_seq"] is not None and max_seq <= src["archived_through_seq"]
            target = conn.execute(sa.text("""
                INSERT INTO conversations (id, team_id, agent_a_id, agent_b_id, a_agent_name, b_agent_name,
                    a_user_id, b_user_id, a_key, b_key, is_default, created_at,
                    archived_at, archived_by_key, archived_through_seq)
                VALUES (:id, :team, :agent_a_id, :agent_b_id, :a_agent_name, :b_agent_name,
                    :a_user_id, :b_user_id, :a_key, :b_key, true, :created_at,
                    :archived_at, :archived_by_key, :archived_through_seq)
                RETURNING id"""), {
                "id": uuid.uuid4(), "team": team, "created_at": first_at,
                **{k: src[k] for k in ("agent_a_id", "agent_b_id", "a_agent_name", "b_agent_name", "a_user_id",
                                        "b_user_id", "a_key", "b_key")},
                "archived_at": src["archived_at"] if covered else None,
                "archived_by_key": src["archived_by_key"] if covered else None,
                "archived_through_seq": max_seq if covered else None,
            }).scalar()
        conn.execute(sa.text(
            "UPDATE messages SET conversation_id = :target, moved_from_conversation_id = :source "
            "WHERE conversation_id = :source AND team_id = :team"),
            {"target": target, "source": source, "team": team})

    op.create_foreign_key("fk_messages_conversation_team", "messages", "conversations",
                          ["conversation_id", "team_id"], ["id", "team_id"])


def downgrade():
    op.drop_constraint("fk_messages_conversation_team", "messages", type_="foreignkey")
    op.drop_constraint("uq_conversations_id_team", "conversations", type_="unique")
    op.drop_index("uq_conversations_default_pair", table_name="conversations")
    # Messages stay where they are; one default thread per pair, the oldest, as before 0008.
    op.execute("""
        UPDATE conversations c SET is_default = false
        WHERE c.is_default AND EXISTS (
            SELECT 1 FROM conversations o
            WHERE o.is_default AND o.a_key = c.a_key AND o.b_key = c.b_key
              AND (o.created_at, o.id) < (c.created_at, c.id))""")
    op.create_index("uq_conversations_default_pair", "conversations", ["a_key", "b_key"], unique=True,
                    postgresql_where=sa.text("is_default"))
    op.drop_column("messages", "moved_from_conversation_id")
    op.drop_column("teams", "attachment_quota_bytes")
    op.drop_column("users", "attachment_quota_bytes")
    op.drop_column("users", "credential_epoch")
