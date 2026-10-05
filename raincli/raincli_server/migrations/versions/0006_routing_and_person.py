"""Messaging as a person and send-to-any-agent routing (protocol §16.2, §16.3, §16.6, §16.10, §16.12).

- machine_agents: reachability on every agent (instant, next-turn, listed) and ``ambiguous``;
- known_agents: deliverable names a machine has reported (send-time routing, step 4);
- agents: the routing policy and when the machine first polled with ``routing=1``;
- messages: person and agent endpoints, ``kind``, and the not-self-endpoint check (C7);
- conversations: two endpoints each, with canonical keys; existing rows become machine pairs;
- person_sessions, handoff_codes, and app-mode web sessions.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NOT_SELF_ENDPOINT = (
    "NOT (sender_agent_id IS NOT DISTINCT FROM recipient_agent_id"
    " AND sender_user_id IS NOT DISTINCT FROM recipient_user_id"
    " AND sender_agent_name IS NOT DISTINCT FROM recipient_agent_name)"
)


def upgrade():
    # Directory: reachability on every agent, plus ambiguous names.
    op.add_column("machine_agents", sa.Column("ambiguous", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.drop_constraint("ck_machine_agents_reachability", "machine_agents")
    op.drop_constraint("ck_machine_agents_inbox_reachability", "machine_agents")
    op.create_check_constraint("ck_machine_agents_reachability", "machine_agents",
                               "reachability IS NULL OR reachability IN ('instant','next-turn','listed')")
    op.create_check_constraint("ck_machine_agents_inbox_reachability", "machine_agents",
                               "role IS NULL OR (reachability IS NOT NULL AND reachability IN ('instant','next-turn'))")
    op.create_check_constraint("ck_machine_agents_ambiguous", "machine_agents", "NOT ambiguous OR reachability IS NOT DISTINCT FROM 'listed'")
    op.create_table(
        "known_agents",
        sa.Column("agent_id", sa.UUID(), sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("reachability", sa.String(16), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reachability IN ('instant','next-turn')", name="ck_known_agents_reachability"),
    )

    # Machines: routing policy and capability.
    op.add_column("agents", sa.Column("routing", sa.String(16), nullable=False, server_default="all"))
    op.add_column("agents", sa.Column("routing_capable_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_agents_routing", "agents", "routing IN ('all','inbox-only')")

    # Messages: endpoints and kind.
    op.alter_column("messages", "sender_agent_id", nullable=True)
    op.alter_column("messages", "recipient_agent_id", nullable=True)
    op.add_column("messages", sa.Column("sender_agent_name", sa.String(64)))
    op.add_column("messages", sa.Column("sender_user_id", sa.UUID(), sa.ForeignKey("users.id")))
    op.add_column("messages", sa.Column("recipient_agent_name", sa.String(64)))
    op.add_column("messages", sa.Column("recipient_user_id", sa.UUID(), sa.ForeignKey("users.id")))
    op.add_column("messages", sa.Column("kind", sa.String(16), nullable=False, server_default="message"))
    op.drop_constraint("ck_messages_not_self", "messages")
    op.create_check_constraint("ck_messages_not_self_endpoint", "messages", NOT_SELF_ENDPOINT)
    op.create_check_constraint("ck_messages_one_sender", "messages",
                               "(sender_agent_id IS NULL) <> (sender_user_id IS NULL)")
    op.create_check_constraint("ck_messages_one_recipient", "messages",
                               "(recipient_agent_id IS NULL) <> (recipient_user_id IS NULL)")
    op.create_check_constraint("ck_messages_sender_name", "messages",
                               "sender_agent_name IS NULL OR sender_agent_id IS NOT NULL")
    op.create_check_constraint("ck_messages_recipient_name", "messages",
                               "recipient_agent_name IS NULL OR recipient_agent_id IS NOT NULL")
    op.create_check_constraint("ck_messages_kind", "messages", "kind IN ('message','escalation')")
    op.create_index("ix_messages_recipient_user_seq", "messages", ["recipient_user_id", "seq"])
    op.create_index("ix_messages_sender_user", "messages", ["sender_user_id"])

    # Conversations: two endpoints with canonical keys. Existing rows are machine pairs.
    op.alter_column("conversations", "agent_a_id", nullable=True)
    op.alter_column("conversations", "agent_b_id", nullable=True)
    for column in ("a_agent_name", "b_agent_name"):
        op.add_column("conversations", sa.Column(column, sa.String(64)))
    for column in ("a_user_id", "b_user_id"):
        op.add_column("conversations", sa.Column(column, sa.UUID(), sa.ForeignKey("users.id")))
    op.add_column("conversations", sa.Column("a_key", sa.String(160)))
    op.add_column("conversations", sa.Column("b_key", sa.String(160)))
    # uuid ordering equals the ordering of their canonical lowercase text, so a < b is preserved.
    op.execute("UPDATE conversations SET a_key = 'm:' || agent_a_id::text, b_key = 'm:' || agent_b_id::text")
    op.alter_column("conversations", "a_key", nullable=False)
    op.alter_column("conversations", "b_key", nullable=False)
    op.drop_index("uq_conversations_default_pair", table_name="conversations")
    op.drop_constraint("ck_conversations_order", "conversations")
    op.create_check_constraint("ck_conversations_order", "conversations", "a_key < b_key")
    op.create_check_constraint("ck_conversations_a_one", "conversations", "(agent_a_id IS NULL) <> (a_user_id IS NULL)")
    op.create_check_constraint("ck_conversations_b_one", "conversations", "(agent_b_id IS NULL) <> (b_user_id IS NULL)")
    op.create_check_constraint("ck_conversations_a_name", "conversations",
                               "a_agent_name IS NULL OR agent_a_id IS NOT NULL")
    op.create_check_constraint("ck_conversations_b_name", "conversations",
                               "b_agent_name IS NULL OR agent_b_id IS NOT NULL")
    op.create_index("uq_conversations_default_pair", "conversations", ["a_key", "b_key"], unique=True,
                    postgresql_where=sa.text("is_default"))
    for name, column in (("ix_conversations_agent_a", "agent_a_id"), ("ix_conversations_agent_b", "agent_b_id"),
                         ("ix_conversations_user_a", "a_user_id"), ("ix_conversations_user_b", "b_user_id")):
        op.create_index(name, "conversations", [column])

    # Person sessions, handoff codes and app-mode web sessions.
    op.create_table(
        "person_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("machine_agent_id", sa.UUID(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prefix", sa.String(12), nullable=False),
        sa.Column("scopes", ARRAY(sa.String(32)), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_person_sessions_user", "person_sessions", ["user_id"])
    op.create_index("ix_person_sessions_machine", "person_sessions", ["machine_agent_id"])
    op.create_table(
        "handoff_codes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("person_session_id", sa.UUID(), sa.ForeignKey("person_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("install_hash", sa.String(64), nullable=False),
    )
    op.add_column("web_sessions", sa.Column("app_mode", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("web_sessions", sa.Column("person_session_id", sa.UUID(),
                                            sa.ForeignKey("person_sessions.id", ondelete="CASCADE")))
    op.create_check_constraint("ck_web_sessions_app_mode", "web_sessions", "app_mode = (person_session_id IS NOT NULL)")
    op.add_column("web_sessions", sa.Column("app_install_hash", sa.String(64)))
    op.create_check_constraint("ck_web_sessions_app_install", "web_sessions", "app_mode = (app_install_hash IS NOT NULL)")


def downgrade():
    # Person and agent endpoints have no v0.4 representation: refuse rather than lose them.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM messages WHERE sender_agent_id IS NULL OR recipient_agent_id IS NULL
                     OR recipient_agent_name IS NOT NULL OR kind <> 'message')
             OR EXISTS (SELECT 1 FROM conversations WHERE agent_a_id IS NULL OR agent_b_id IS NULL
                        OR a_agent_name IS NOT NULL OR b_agent_name IS NOT NULL) THEN
            RAISE EXCEPTION 'cannot downgrade below 0006: person or agent endpoints exist';
          END IF;
        END $$""")
    # §16.14 S5: an app-mode session must never survive as a full web session once its columns are gone.
    op.execute("DELETE FROM web_sessions WHERE app_mode")
    op.drop_constraint("ck_web_sessions_app_install", "web_sessions")
    op.drop_column("web_sessions", "app_install_hash")
    op.drop_constraint("ck_web_sessions_app_mode", "web_sessions")
    op.drop_column("web_sessions", "person_session_id")
    op.drop_column("web_sessions", "app_mode")
    op.drop_table("handoff_codes")
    op.drop_index("ix_person_sessions_machine", table_name="person_sessions")
    op.drop_index("ix_person_sessions_user", table_name="person_sessions")
    op.drop_table("person_sessions")

    for name in ("ix_conversations_agent_a", "ix_conversations_agent_b", "ix_conversations_user_a",
                 "ix_conversations_user_b", "uq_conversations_default_pair"):
        op.drop_index(name, table_name="conversations")
    for name in ("ck_conversations_a_one", "ck_conversations_b_one", "ck_conversations_a_name",
                 "ck_conversations_b_name", "ck_conversations_order"):
        op.drop_constraint(name, "conversations")
    for column in ("a_key", "b_key", "a_user_id", "b_user_id", "a_agent_name", "b_agent_name"):
        op.drop_column("conversations", column)
    op.alter_column("conversations", "agent_a_id", nullable=False)
    op.alter_column("conversations", "agent_b_id", nullable=False)
    op.create_check_constraint("ck_conversations_order", "conversations", "agent_a_id < agent_b_id")
    op.create_index("uq_conversations_default_pair", "conversations", ["agent_a_id", "agent_b_id"], unique=True,
                    postgresql_where=sa.text("is_default"))

    op.drop_index("ix_messages_sender_user", table_name="messages")
    op.drop_index("ix_messages_recipient_user_seq", table_name="messages")
    for name in ("ck_messages_kind", "ck_messages_recipient_name", "ck_messages_sender_name",
                 "ck_messages_one_recipient", "ck_messages_one_sender", "ck_messages_not_self_endpoint"):
        op.drop_constraint(name, "messages")
    for column in ("kind", "recipient_user_id", "recipient_agent_name", "sender_user_id", "sender_agent_name"):
        op.drop_column("messages", column)
    op.alter_column("messages", "sender_agent_id", nullable=False)
    op.alter_column("messages", "recipient_agent_id", nullable=False)
    op.create_check_constraint("ck_messages_not_self", "messages", "sender_agent_id <> recipient_agent_id")

    op.drop_constraint("ck_agents_routing", "agents")
    op.drop_column("agents", "routing_capable_at")
    op.drop_column("agents", "routing")

    op.drop_table("known_agents")
    op.drop_constraint("ck_machine_agents_ambiguous", "machine_agents")
    op.drop_constraint("ck_machine_agents_inbox_reachability", "machine_agents")
    op.drop_constraint("ck_machine_agents_reachability", "machine_agents")
    # Non-inbox reachability had no v0.4 representation; drop it before restoring the old checks.
    op.execute("UPDATE machine_agents SET reachability = NULL WHERE role IS NULL")
    op.create_check_constraint("ck_machine_agents_reachability", "machine_agents",
                               "reachability IS NULL OR (role = 'inbox' AND reachability IN ('instant','next-turn'))")
    op.create_check_constraint("ck_machine_agents_inbox_reachability", "machine_agents",
                               "(role IS NULL) = (reachability IS NULL)")
    op.drop_column("machine_agents", "ambiguous")
