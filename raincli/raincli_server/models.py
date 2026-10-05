"""PostgreSQL schema (protocol §1-§2). Migrations in raincli_server/migrations mirror this."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DELIVERY_STATES = ("stored", "received", "held", "submitted", "submission_uncertain", "rejected", "replied")
MESSAGE_KINDS = ("message", "escalation")
ROUTING_POLICIES = ("all", "inbox-only")
PERSON_SCOPES = ("person:read", "person:send")
EVENT_STATES = ("held", "submitted", "submission_uncertain", "rejected")
SCOPES = ("messages:read", "messages:send", "messages:ack")
ROLES = ("owner", "member")


# §16.12 C7: the sender endpoint (machine, from_agent or null; or a person) differs from the recipient's.
NOT_SELF_ENDPOINT = (
    "NOT (sender_agent_id IS NOT DISTINCT FROM recipient_agent_id"
    " AND sender_user_id IS NOT DISTINCT FROM recipient_user_id"
    " AND sender_agent_name IS NOT DISTINCT FROM recipient_agent_name)"
)


class Base(DeclarativeBase):
    pass


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid()
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (Index("uq_users_email_lower", func.lower(email), unique=True),)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = _uuid()
    slug: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = _created()


class Membership(Base):
    __tablename__ = "memberships"
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    created_at: Mapped[datetime] = _created()
    __table_args__ = (CheckConstraint("role IN ('owner','member')", name="ck_memberships_role"),)


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = _uuid()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # App-mode sessions come from a person-session handoff (§16.10, §16.12 C2/C3): they carry only the
    # person scopes, end no later than the person session and are revoked with it.
    app_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    person_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person_sessions.id", ondelete="CASCADE"))
    __table_args__ = (
        CheckConstraint("app_mode = (person_session_id IS NOT NULL)", name="ck_web_sessions_app_mode"),
    )


class PersonSession(Base):
    """A person session ``rps_…`` issued to a signed-in machine (protocol §16.3). Stored hashed."""

    __tablename__ = "person_sessions"
    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    machine_agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_person_sessions_user", "user_id"), Index("ix_person_sessions_machine", "machine_agent_id"))


class HandoffCode(Base):
    """A single-use, 60-second code that turns a person session into an app-mode web session (§16.10)."""

    __tablename__ = "handoff_codes"
    id: Mapped[uuid.UUID] = _uuid()
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    person_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("person_sessions.id", ondelete="CASCADE"),
                                                         nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[uuid.UUID] = _uuid()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    handle: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The machine name given to POST /api/v1/app/login when sign-in created this machine (§15.7).
    signed_in_from: Mapped[str | None] = mapped_column(String(32))
    # The last credential rotation and who made it (protocol §15.8 H2).
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_by: Mapped[str | None] = mapped_column(String(16))
    # When the machine first published an inbox role (§15.8 H2); backfilled conservatively by 0005.
    inbox_role_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 2 (§16.5, §16.2): the machine's routing policy, and when it first polled with routing=1.
    routing: Mapped[str] = mapped_column(String(16), nullable=False, default="all", server_default="all")
    routing_capable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("team_id", "handle", name="uq_agents_team_handle"),
        CheckConstraint("rotated_by IS NULL OR rotated_by IN ('app-login','website','operator')",
                        name="ck_agents_rotated_by"),
        CheckConstraint("routing IN ('all','inbox-only')", name="ck_agents_routing"),
    )


class AgentPresence(Base):
    __tablename__ = "agent_presence"
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Machine client report (protocol §14.1); null for clients older than v0.3.0.
    client_version: Mapped[str | None] = mapped_column(String(32))
    update_mode: Mapped[str | None] = mapped_column(String(16))
    update_state: Mapped[str | None] = mapped_column(String(16))
    update_error: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (
        CheckConstraint("status IN ('ready','busy','blocked','offline','unknown')", name="ck_agent_presence_status"),
        CheckConstraint("update_mode IS NULL OR update_mode IN ('automatic','manual')", name="ck_agent_presence_mode"),
        CheckConstraint("update_state IS NULL OR update_state IN ('current','updating','failed','rolled_back')",
                        name="ck_agent_presence_update_state"),
    )


class MachineAgent(Base):
    """One agent session published by a machine's runtime (protocol §14.1). Snapshot-replaced."""

    __tablename__ = "machine_agents"
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str | None] = mapped_column(String(16))
    reachability: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # §16.2: a name that occurs twice among the machine's deliverable agents is reported listed + ambiguous.
    ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    __table_args__ = (
        CheckConstraint("status IN ('working','idle','blocked','offline','unknown')", name="ck_machine_agents_status"),
        CheckConstraint("type IN ('claude','codex','gemini','cursor','opencode','other')", name="ck_machine_agents_type"),
        CheckConstraint("role IS NULL OR role = 'inbox'", name="ck_machine_agents_role"),
        # Every agent may carry reachability (§16.2); the inbox must be deliverable, as before.
        CheckConstraint("reachability IS NULL OR reachability IN ('instant','next-turn','listed')",
                        name="ck_machine_agents_reachability"),
        CheckConstraint("source IN ('herdr','hook','scan')", name="ck_machine_agents_source"),
        CheckConstraint("role IS NULL OR (reachability IS NOT NULL AND reachability IN ('instant','next-turn'))",
                        name="ck_machine_agents_inbox_reachability"),
        CheckConstraint("NOT ambiguous OR reachability IS NOT DISTINCT FROM 'listed'", name="ck_machine_agents_ambiguous"),
        Index("uq_machine_agents_one_inbox", "agent_id", unique=True, postgresql_where="role = 'inbox'"),
    )


class KnownAgent(Base):
    """A deliverable agent name a machine reported within the retention window (§16.2, §16.12 C12)."""

    __tablename__ = "known_agents"
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    reachability: Mapped[str] = mapped_column(String(16), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("reachability IN ('instant','next-turn')", name="ck_known_agents_reachability"),
    )


class ClientTarget(Base):
    """Operator-set client version for a team (protocol §14.5). Names a version, never a source."""

    __tablename__ = "client_targets"
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    allow_downgrade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    set_at: Mapped[datetime] = _created()
    __table_args__ = (CheckConstraint("version ~ '^v(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})$'", name="ck_client_targets_version"),)


class AgentCredential(Base):
    __tablename__ = "agent_credentials"
    id: Mapped[uuid.UUID] = _uuid()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    """A thread between exactly two endpoints (§16.6). Each side is a machine, an agent on a machine
    (machine plus name) or a person. ``a_key``/``b_key`` are canonical endpoint keys (``m:<id>``,
    ``a:<id>:<name>``, ``p:<id>``) with ``a_key < b_key``, so a pair has one default thread."""

    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = _uuid()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    agent_a_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    agent_b_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    a_agent_name: Mapped[str | None] = mapped_column(String(64))
    b_agent_name: Mapped[str | None] = mapped_column(String(64))
    a_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    b_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    a_key: Mapped[str] = mapped_column(String(160), nullable=False)
    b_key: Mapped[str] = mapped_column(String(160), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        CheckConstraint("a_key < b_key", name="ck_conversations_order"),
        CheckConstraint("(agent_a_id IS NULL) <> (a_user_id IS NULL)", name="ck_conversations_a_one"),
        CheckConstraint("(agent_b_id IS NULL) <> (b_user_id IS NULL)", name="ck_conversations_b_one"),
        CheckConstraint("a_agent_name IS NULL OR agent_a_id IS NOT NULL", name="ck_conversations_a_name"),
        CheckConstraint("b_agent_name IS NULL OR agent_b_id IS NOT NULL", name="ck_conversations_b_name"),
        Index("uq_conversations_default_pair", "a_key", "b_key", unique=True, postgresql_where="is_default"),
        Index("ix_conversations_agent_a", "agent_a_id"),
        Index("ix_conversations_agent_b", "agent_b_id"),
        Index("ix_conversations_user_a", "a_user_id"),
        Index("ix_conversations_user_b", "b_user_id"),
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    in_reply_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    # Endpoints (§16.1): a machine (agent id), an agent on a machine (agent id plus name) or a person.
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    sender_agent_name: Mapped[str | None] = mapped_column(String(64))  # from_agent, as stated by the machine
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    recipient_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    recipient_agent_name: Mapped[str | None] = mapped_column(String(64))
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="message", server_default="message")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_state: Mapped[str] = mapped_column(String(24), nullable=False, default="stored")
    delivery_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint(NOT_SELF_ENDPOINT, name="ck_messages_not_self_endpoint"),
        CheckConstraint("(sender_agent_id IS NULL) <> (sender_user_id IS NULL)", name="ck_messages_one_sender"),
        CheckConstraint("(recipient_agent_id IS NULL) <> (recipient_user_id IS NULL)", name="ck_messages_one_recipient"),
        CheckConstraint("sender_agent_name IS NULL OR sender_agent_id IS NOT NULL", name="ck_messages_sender_name"),
        CheckConstraint("recipient_agent_name IS NULL OR recipient_agent_id IS NOT NULL",
                        name="ck_messages_recipient_name"),
        CheckConstraint("kind IN ('message','escalation')", name="ck_messages_kind"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 16000", name="ck_messages_body_len"),
        Index("ix_messages_recipient_seq", "recipient_agent_id", "seq"),
        Index("ix_messages_recipient_pending", "recipient_agent_id", postgresql_where="acked_at IS NULL"),
        Index("ix_messages_recipient_user_seq", "recipient_user_id", "seq"),
        Index("ix_messages_sender_user", "sender_user_id"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )


class DeliveryEvent(Base):
    __tablename__ = "delivery_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500))
    reported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agents.id"))
    created_at: Mapped[datetime] = _created()
    __table_args__ = (Index("ix_delivery_events_message", "message_id", "id"),)


class Attachment(Base):
    """Markdown file linked to one message; exact bytes, stored with the message atomically."""

    __tablename__ = "attachments"
    id: Mapped[uuid.UUID] = _uuid()
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(100), nullable=False)
    media_type: Mapped[str] = mapped_column(String(40), nullable=False, default="text/markdown")
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        UniqueConstraint("message_id", "position", name="uq_attachments_message_position"),
        Index("uq_attachments_message_filename", "message_id", func.lower(filename), unique=True),
        CheckConstraint("size = octet_length(content)", name="ck_attachments_size"),
        CheckConstraint("size BETWEEN 1 AND 262144", name="ck_attachments_size_bound"),
    )
