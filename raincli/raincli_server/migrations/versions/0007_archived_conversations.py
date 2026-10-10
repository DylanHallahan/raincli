"""Archived conversations (protocol §17.2, §17.3 A1, A7).

- conversations.archived_at, archived_by_key (the endpoint key, or ``p:<user id>``, that archived it) and
  archived_through_seq (the newest message seq the archiver had seen). A conversation is archived only while
  no message in it has a higher seq, so the send path never writes here;
- partial indexes on the endpoint list columns ``WHERE archived_at IS NULL``, so the main lists stay
  index-served (the archived view uses the existing endpoint indexes).

Nothing is deleted when a conversation is archived; the downgrade drops only these columns and indexes.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

ENDPOINT_COLUMNS = ("agent_a_id", "agent_b_id", "a_user_id", "b_user_id")


def upgrade():
    op.add_column("conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("archived_by_key", sa.String(160), nullable=True))
    op.add_column("conversations", sa.Column("archived_through_seq", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_conversations_archived", "conversations",
        "(archived_at IS NULL) = (archived_by_key IS NULL) AND (archived_at IS NULL) = (archived_through_seq IS NULL)")
    for column in ENDPOINT_COLUMNS:
        op.create_index(f"ix_conversations_{column}_active", "conversations", [column],
                        postgresql_where=sa.text("archived_at IS NULL"))


def downgrade():
    for column in ENDPOINT_COLUMNS:
        op.drop_index(f"ix_conversations_{column}_active", table_name="conversations")
    op.drop_constraint("ck_conversations_archived", "conversations")
    op.drop_column("conversations", "archived_through_seq")
    op.drop_column("conversations", "archived_by_key")
    op.drop_column("conversations", "archived_at")
