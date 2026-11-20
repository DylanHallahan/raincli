"""Account switching (protocol §23): grants and delegated person sessions.

Run with the service stopped; Alembic runs it in one transaction.

- ``grants``: the operator lets a person (the grantee) act as another person of the same team (the account), or, with
  ``all_members`` and no grantee, every active member of the team but the account (a team-wide grant). Keys go to
  ``users`` and ``teams`` separately, never to memberships, so a member's removal can still delete their membership:
  active membership is checked where a grant is added and used. A grant is revoked, never deleted (the audit); one live
  grant per (team, grantee, account), and one live team-wide grant per (team, account). ``toasts``: whether the app
  shows toasts for an account used through the grant (A18: on for per-user grants, off for team-wide ones).
- ``person_sessions``: a delegated session has ``user_id`` = the account, the grantee as ``acting_user_id``, its
  ``grant_id`` and the grantee's own session as ``parent_session_id``, all three or none.
- ``messages.acting_user_id`` and ``messages.grant_id``: the grantee, and the grant in effect at send time, on a
  person send made through a delegated session (set together; never shown to recipients).

The downgrade deletes every delegated session (with the handoff codes and app-mode web sessions made from it) and
every grant, then drops what this revision added. The acting person and grant recorded on messages are lost. Data from before
the upgrade is unchanged.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

DELEGATED = "person_sessions WHERE acting_user_id IS NOT NULL"


def upgrade():
    op.create_table(
        "grants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("grantee_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("account_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("all_members", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("toasts", sa.Boolean(), nullable=False),
        sa.CheckConstraint("grantee_user_id <> account_user_id", name="ck_grants_not_self"),
        sa.CheckConstraint("all_members = (grantee_user_id IS NULL)", name="ck_grants_all_members"),
    )
    op.create_index("uq_grants_live", "grants", ["team_id", "grantee_user_id", "account_user_id"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL"))
    op.create_index("uq_grants_live_all_members", "grants", ["team_id", "account_user_id"], unique=True,
                    postgresql_where=sa.text("all_members AND revoked_at IS NULL"))
    op.create_index("ix_grants_account", "grants", ["account_user_id", "team_id"])

    op.add_column("person_sessions", sa.Column("acting_user_id", UUID(as_uuid=True), nullable=True))
    op.add_column("person_sessions", sa.Column("grant_id", UUID(as_uuid=True), nullable=True))
    op.add_column("person_sessions", sa.Column("parent_session_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_person_sessions_acting_user", "person_sessions", "users", ["acting_user_id"], ["id"],
                          ondelete="CASCADE")
    op.create_foreign_key("fk_person_sessions_grant", "person_sessions", "grants", ["grant_id"], ["id"])
    op.create_foreign_key("fk_person_sessions_parent", "person_sessions", "person_sessions", ["parent_session_id"],
                          ["id"], ondelete="CASCADE")
    op.create_check_constraint(
        "ck_person_sessions_delegated", "person_sessions",
        "(acting_user_id IS NULL) = (grant_id IS NULL) AND (acting_user_id IS NULL) = (parent_session_id IS NULL) "
        "AND (acting_user_id IS NULL OR acting_user_id <> user_id)")
    op.create_index("ix_person_sessions_parent", "person_sessions", ["parent_session_id"],
                    postgresql_where=sa.text("parent_session_id IS NOT NULL"))
    op.create_index("ix_person_sessions_grant", "person_sessions", ["grant_id"],
                    postgresql_where=sa.text("grant_id IS NOT NULL"))

    op.add_column("messages", sa.Column("acting_user_id", UUID(as_uuid=True), nullable=True))
    op.add_column("messages", sa.Column("grant_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_messages_acting_user", "messages", "users", ["acting_user_id"], ["id"])
    op.create_foreign_key("fk_messages_grant", "messages", "grants", ["grant_id"], ["id"])
    op.create_check_constraint("ck_messages_acting_person", "messages",
                               "acting_user_id IS NULL OR sender_user_id IS NOT NULL")
    op.create_check_constraint("ck_messages_acting_grant", "messages", "(acting_user_id IS NULL) = (grant_id IS NULL)")
    op.create_index("ix_messages_acting", "messages", ["acting_user_id", "created_at"],
                    postgresql_where=sa.text("acting_user_id IS NOT NULL"))


def downgrade():
    op.execute(f"DELETE FROM handoff_codes WHERE person_session_id IN (SELECT id FROM {DELEGATED})")
    op.execute(f"DELETE FROM web_sessions WHERE person_session_id IN (SELECT id FROM {DELEGATED})")
    op.execute(f"DELETE FROM {DELEGATED}")

    op.drop_index("ix_messages_acting", table_name="messages")
    op.drop_constraint("ck_messages_acting_grant", "messages", type_="check")
    op.drop_constraint("ck_messages_acting_person", "messages", type_="check")
    op.drop_constraint("fk_messages_grant", "messages", type_="foreignkey")
    op.drop_constraint("fk_messages_acting_user", "messages", type_="foreignkey")
    op.drop_column("messages", "grant_id")
    op.drop_column("messages", "acting_user_id")

    op.drop_index("ix_person_sessions_grant", table_name="person_sessions")
    op.drop_index("ix_person_sessions_parent", table_name="person_sessions")
    op.drop_constraint("ck_person_sessions_delegated", "person_sessions", type_="check")
    op.drop_constraint("fk_person_sessions_parent", "person_sessions", type_="foreignkey")
    op.drop_constraint("fk_person_sessions_grant", "person_sessions", type_="foreignkey")
    op.drop_constraint("fk_person_sessions_acting_user", "person_sessions", type_="foreignkey")
    op.drop_column("person_sessions", "parent_session_id")
    op.drop_column("person_sessions", "grant_id")
    op.drop_column("person_sessions", "acting_user_id")

    op.drop_index("ix_grants_account", table_name="grants")
    op.drop_index("uq_grants_live_all_members", table_name="grants")
    op.drop_index("uq_grants_live", table_name="grants")
    op.drop_table("grants")
