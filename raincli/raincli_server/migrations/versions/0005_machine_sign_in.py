"""Machine sign-in (protocol §15.7, §15.8 H2): the name a sign-in created a machine from, the last
credential rotation and who made it, and when a machine first published an inbox role."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("signed_in_from", sa.String(32)))
    op.add_column("agents", sa.Column("rotated_at", sa.DateTime(timezone=True)))
    op.add_column("agents", sa.Column("rotated_by", sa.String(16)))
    op.add_column("agents", sa.Column("inbox_role_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_agents_rotated_by", "agents",
                               "rotated_by IS NULL OR rotated_by IN ('app-login','website','operator')")
    # Inbox roles were never kept historically, so any machine that has ever reported presence or used
    # its credential counts as having delivery history: app sign-in can then replace it only with proof
    # of its current credential (protocol §15.8 H2).
    op.execute("""
        UPDATE agents SET inbox_role_at = now()
        WHERE EXISTS (SELECT 1 FROM agent_presence p WHERE p.agent_id = agents.id)
           OR EXISTS (SELECT 1 FROM agent_credentials c WHERE c.agent_id = agents.id AND c.last_used_at IS NOT NULL)
    """)


def downgrade():
    op.drop_constraint("ck_agents_rotated_by", "agents")
    for column in ("inbox_role_at", "rotated_by", "rotated_at", "signed_in_from"):
        op.drop_column("agents", column)
