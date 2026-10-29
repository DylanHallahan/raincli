"""Group chats (protocol §22): group conversations, their members, posts and the per-member copies.

Run with the service stopped; Alembic runs it in one transaction.

- ``conversations``: ``kind`` gains ``group``, with a ``name`` (1–40 characters, NFC, no control, line-break or
  bidi-override characters, unique per team without case). A group row has no side columns, the keys
  ``group:<id>`` and ``group:<id>:``, is never default, archived or merged. The pair and own checks hold for every
  other kind as before.
- ``group_members``: (group, team, person, joined_at); the person must be a member of the team (no cascade: the
  removal paths delete these rows themselves, withdrawing that person's copies).
- ``group_posts``: each post once, numbered from ``teams.next_seq`` by the same trigger as messages (0011).
- ``messages``: a copy of a post is an ordinary message with ``route = 'group'`` and ``post_id``, always to a machine;
  ``withdrawn_at`` marks a copy that will never be delivered.
- ``agents.groups_capable_at``: when the machine last qualified to receive copies (a ``groups=1`` poll on a client
  of at least the groups version).

The downgrade deletes every group copy (and its delivery events), post, member and group conversation, then drops
what this revision added and restores the checks. Data from before the upgrade is unchanged; group data written
after it is lost.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

OWN_SHAPE = ("(kind = 'pair') = (b_key NOT LIKE 'self:%') AND (kind = 'pair' OR ("
             "agent_a_id IS NULL AND agent_b_id IS NULL AND a_user_id = b_user_id "
             "AND a_key = 'p:' || a_user_id::text AND b_key = 'self:' || a_user_id::text))")
A_ONE = "(agent_a_id IS NULL) <> (a_user_id IS NULL)"
B_ONE = "(agent_b_id IS NULL) <> (b_user_id IS NULL)"
OLD_KINDS = "kind IN ('pair','self','own_agents')"
NEW_KINDS = "kind IN ('pair','self','own_agents','group')"
# Characters a group name may not contain (regex escapes, read by PostgreSQL's regex engine): C0/C1 controls, line
# and paragraph separators, bidi controls. A NUL can't be stored in text at all.
NAME_FORBIDDEN = "[\\x01-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
GROUP_SHAPE = ("kind <> 'group' OR (agent_a_id IS NULL AND agent_b_id IS NULL AND a_user_id IS NULL "
               "AND b_user_id IS NULL AND a_agent_name IS NULL AND b_agent_name IS NULL "
               "AND a_key = 'group:' || id::text AND b_key = 'group:' || id::text || ':' "
               "AND NOT is_default AND archived_at IS NULL AND merged_into IS NULL)")
GROUP_NAME = (f"name IS NULL OR (char_length(name) BETWEEN 1 AND 40 AND name = btrim(name) "
              f"AND name = normalize(name, NFC) AND name !~ '{NAME_FORBIDDEN}')")
RELAXED = (("ck_conversations_own_shape", OWN_SHAPE), ("ck_conversations_a_one", A_ONE),
           ("ck_conversations_b_one", B_ONE))


def upgrade():
    op.add_column("conversations", sa.Column("name", sa.String(40), nullable=True))
    op.drop_constraint("ck_conversations_kind", "conversations", type_="check")
    op.create_check_constraint("ck_conversations_kind", "conversations", NEW_KINDS)
    for name, check in RELAXED:
        op.drop_constraint(name, "conversations", type_="check")
        op.create_check_constraint(name, "conversations", f"kind = 'group' OR ({check})")
    op.create_check_constraint("ck_conversations_group_name", "conversations", "(kind = 'group') = (name IS NOT NULL)")
    op.create_check_constraint("ck_conversations_group_name_text", "conversations", GROUP_NAME)
    op.create_check_constraint("ck_conversations_group_shape", "conversations", GROUP_SHAPE)
    op.create_index("uq_conversations_group_name", "conversations", ["team_id", sa.text("lower(name)")], unique=True,
                    postgresql_where=sa.text("kind = 'group'"))

    op.create_table(
        "group_members",
        sa.Column("conversation_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("team_id", UUID(as_uuid=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id", "team_id"], ["conversations.id", "conversations.team_id"],
                                name="fk_group_members_group_team"),
        sa.ForeignKeyConstraint(["team_id", "user_id"], ["memberships.team_id", "memberships.user_id"],
                                name="fk_group_members_membership"),
    )
    op.create_index("ix_group_members_user", "group_members", ["user_id", "team_id"])

    op.create_table(
        "group_posts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("team_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("team_seq", sa.BigInteger(), nullable=False),
        sa.Column("author_agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("author_agent_name", sa.String(64), nullable=True),
        sa.Column("author_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("mentions", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("in_reply_to_post", UUID(as_uuid=True), sa.ForeignKey("group_posts.id"), nullable=True),
        sa.Column("recipients_m", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id", "team_id"], ["conversations.id", "conversations.team_id"],
                                name="fk_group_posts_group_team"),
        sa.ForeignKeyConstraint(["author_agent_id", "team_id"], ["agents.id", "agents.team_id"],
                                name="fk_group_posts_author_team"),
        sa.UniqueConstraint("team_id", "team_seq", name="uq_group_posts_team_seq"),
        sa.CheckConstraint("char_length(body) BETWEEN 1 AND 16000", name="ck_group_posts_body_len"),
        sa.CheckConstraint("recipients_m >= 0", name="ck_group_posts_recipients_m"),
    )
    op.create_index("ix_group_posts_group_seq", "group_posts", ["conversation_id", "team_seq"])
    op.execute("CREATE TRIGGER trg_group_posts_team_seq BEFORE INSERT ON group_posts "
               "FOR EACH ROW EXECUTE FUNCTION messages_allocate_team_seq()")

    op.add_column("messages", sa.Column("post_id", UUID(as_uuid=True), nullable=True))
    op.add_column("messages", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_messages_post", "messages", "group_posts", ["post_id"], ["id"])
    op.drop_constraint("ck_messages_route", "messages", type_="check")
    op.create_check_constraint("ck_messages_route", "messages", "route IN ('inbox','reply','person','self','group')")
    op.create_check_constraint("ck_messages_group_post", "messages",
                               "(route IS NOT DISTINCT FROM 'group') = (post_id IS NOT NULL)")
    op.create_check_constraint("ck_messages_group_to_machine", "messages",
                               "route <> 'group' OR (recipient_agent_id IS NOT NULL AND recipient_user_id IS NULL)")
    op.create_check_constraint("ck_messages_withdrawn_group", "messages",
                               "withdrawn_at IS NULL OR route IS NOT DISTINCT FROM 'group'")
    op.create_index("ix_messages_post", "messages", ["post_id"], postgresql_where=sa.text("post_id IS NOT NULL"))

    op.add_column("agents", sa.Column("groups_capable_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.execute("DELETE FROM delivery_events WHERE message_id IN (SELECT id FROM messages WHERE route = 'group')")
    op.execute("DELETE FROM messages WHERE route = 'group'")
    op.execute("DELETE FROM group_posts")
    op.execute("DELETE FROM group_members")
    op.execute("DELETE FROM conversations WHERE kind = 'group'")

    op.drop_column("agents", "groups_capable_at")
    op.drop_index("ix_messages_post", table_name="messages")
    op.drop_constraint("ck_messages_withdrawn_group", "messages", type_="check")
    op.drop_constraint("ck_messages_group_to_machine", "messages", type_="check")
    op.drop_constraint("ck_messages_group_post", "messages", type_="check")
    op.drop_constraint("ck_messages_route", "messages", type_="check")
    op.create_check_constraint("ck_messages_route", "messages", "route IN ('inbox','reply','person','self')")
    op.drop_constraint("fk_messages_post", "messages", type_="foreignkey")
    op.drop_column("messages", "withdrawn_at")
    op.drop_column("messages", "post_id")

    op.execute("DROP TRIGGER trg_group_posts_team_seq ON group_posts")
    op.drop_index("ix_group_posts_group_seq", table_name="group_posts")
    op.drop_table("group_posts")
    op.drop_index("ix_group_members_user", table_name="group_members")
    op.drop_table("group_members")

    op.drop_index("uq_conversations_group_name", table_name="conversations")
    op.drop_constraint("ck_conversations_group_shape", "conversations", type_="check")
    op.drop_constraint("ck_conversations_group_name_text", "conversations", type_="check")
    op.drop_constraint("ck_conversations_group_name", "conversations", type_="check")
    for name, check in RELAXED:
        op.drop_constraint(name, "conversations", type_="check")
        op.create_check_constraint(name, "conversations", check)
    op.drop_constraint("ck_conversations_kind", "conversations", type_="check")
    op.create_check_constraint("ck_conversations_kind", "conversations", OLD_KINDS)
    op.drop_column("conversations", "name")
