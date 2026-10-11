"""The pending-message index orders by seq (protocol §19.6).

``ix_messages_recipient_pending`` on ``(recipient_agent_id) WHERE acked_at IS NULL`` becomes
``(recipient_agent_id, seq) WHERE acked_at IS NULL``: it still serves the per-recipient pending count, and the
oldest unacknowledged message to a machine (the sender warning's "not picking up") is one index lookup.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_messages_recipient_pending", table_name="messages")
    op.create_index("ix_messages_recipient_pending", "messages", ["recipient_agent_id", "seq"],
                    postgresql_where="acked_at IS NULL")


def downgrade():
    op.drop_index("ix_messages_recipient_pending", table_name="messages")
    op.create_index("ix_messages_recipient_pending", "messages", ["recipient_agent_id"],
                    postgresql_where="acked_at IS NULL")
