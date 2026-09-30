"""Machine agent directory, client version report and team client targets."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_presence", sa.Column("client_version", sa.String(32)))
    op.add_column("agent_presence", sa.Column("update_mode", sa.String(16)))
    op.add_column("agent_presence", sa.Column("update_state", sa.String(16)))
    op.add_column("agent_presence", sa.Column("update_error", sa.String(200)))
    op.create_check_constraint("ck_agent_presence_mode", "agent_presence",
                               "update_mode IS NULL OR update_mode IN ('automatic','manual')")
    op.create_check_constraint("ck_agent_presence_update_state", "agent_presence",
                               "update_state IS NULL OR update_state IN ('current','updating','failed','rolled_back')")
    op.create_table("machine_agents",
        sa.Column("agent_id", sa.UUID(), sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16)),
        sa.Column("reachability", sa.String(16)),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('working','idle','blocked','offline','unknown')", name="ck_machine_agents_status"),
        sa.CheckConstraint("type IN ('claude','codex','gemini','cursor','opencode','other')", name="ck_machine_agents_type"),
        sa.CheckConstraint("role IS NULL OR role = 'inbox'", name="ck_machine_agents_role"),
        sa.CheckConstraint("reachability IS NULL OR (role = 'inbox' AND reachability IN ('instant','next-turn'))",
                           name="ck_machine_agents_reachability"),
        sa.CheckConstraint("source IN ('herdr','hook','scan')", name="ck_machine_agents_source"))
    op.create_index("uq_machine_agents_one_inbox", "machine_agents", ["agent_id"], unique=True,
                    postgresql_where=sa.text("role = 'inbox'"))
    op.create_table("client_targets",
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("allow_downgrade", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(r"version ~ '^v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'", name="ck_client_targets_version"))


def downgrade():
    op.drop_table("client_targets")
    op.drop_index("uq_machine_agents_one_inbox", table_name="machine_agents")
    op.drop_table("machine_agents")
    op.drop_constraint("ck_agent_presence_update_state", "agent_presence")
    op.drop_constraint("ck_agent_presence_mode", "agent_presence")
    for column in ("update_error", "update_state", "update_mode", "client_version"):
        op.drop_column("agent_presence", column)
