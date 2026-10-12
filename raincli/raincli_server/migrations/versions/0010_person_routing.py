"""Person routing (protocol §21): the inbox setting, person-pair conversations and redelivery.

Run with the service stopped; Alembic runs it in one transaction.

- person_inboxes: per person and team, the (machine, agent name) that receives their teammates' messages;
- conversations.kind: ``pair`` (two people), ``self`` (a person's own agents writing to them) or ``own_agents``
  (a person's machine-to-machine history, private); ``merged_into`` and ``premerge_is_default`` for the merge;
- messages.redeliver (re-targeted, returned below the poll cursor until acked), messages.route (how the
  recipient was chosen) and messages.premerge_conversation_id (for the downgrade).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

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
    op.create_check_constraint("ck_messages_route", "messages", "route IN ('inbox','reply','person','self')")
    for column in ("agent", "user"):
        op.create_index(f"ix_messages_redeliver_{column}", "messages", [f"recipient_{column}_id", "seq"],
                        postgresql_where=sa.text("redeliver AND acked_at IS NULL"))


def downgrade():
    for column in ("agent", "user"):
        op.drop_index(f"ix_messages_redeliver_{column}", table_name="messages")
    op.drop_constraint("ck_messages_route", "messages", type_="check")
    op.drop_column("messages", "route")
    op.drop_column("messages", "redeliver")
    op.drop_column("messages", "premerge_conversation_id")
    op.drop_index("ix_conversations_merged_into", table_name="conversations")
    for kind in ("self", "own_agents"):
        op.drop_index(f"uq_conversations_{kind}", table_name="conversations")
    op.drop_constraint("ck_conversations_own_shape", "conversations", type_="check")
    op.drop_constraint("ck_conversations_kind", "conversations", type_="check")
    op.drop_constraint("fk_conversations_merged_into", "conversations", type_="foreignkey")
    op.drop_column("conversations", "premerge_is_default")
    op.drop_column("conversations", "merged_into")
    op.drop_column("conversations", "kind")
    op.drop_index("ix_person_inboxes_agent", table_name="person_inboxes")
    op.drop_table("person_inboxes")
