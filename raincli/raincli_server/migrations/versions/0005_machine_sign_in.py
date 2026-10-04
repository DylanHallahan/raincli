"""Machines created by app sign-in record the machine name they signed in from (protocol §15.7)."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("signed_in_from", sa.String(32)))


def downgrade():
    op.drop_column("agents", "signed_in_from")
