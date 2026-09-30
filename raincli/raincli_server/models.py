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
EVENT_STATES = ("held", "submitted", "submission_uncertain", "rejected")
SCOPES = ("messages:read", "messages:send", "messages:ack")
ROLES = ("owner", "member")


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


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[uuid.UUID] = _uuid()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    handle: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("team_id", "handle", name="uq_agents_team_handle"),)


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
    __table_args__ = (
        CheckConstraint("status IN ('working','idle','blocked','offline','unknown')", name="ck_machine_agents_status"),
        CheckConstraint("type IN ('claude','codex','gemini','cursor','opencode','other')", name="ck_machine_agents_type"),
        CheckConstraint("role IS NULL OR role = 'inbox'", name="ck_machine_agents_role"),
        CheckConstraint("reachability IS NULL OR (role = 'inbox' AND reachability IN ('instant','next-turn'))",
                        name="ck_machine_agents_reachability"),
        CheckConstraint("source IN ('herdr','hook','scan')", name="ck_machine_agents_source"),
        CheckConstraint("(role IS NULL) = (reachability IS NULL)", name="ck_machine_agents_inbox_reachability"),
        Index("uq_machine_agents_one_inbox", "agent_id", unique=True, postgresql_where="role = 'inbox'"),
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
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = _uuid()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    # Direct thread: agent_a_id < agent_b_id (canonical order) so a pair has one default thread.
    agent_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    agent_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (
        CheckConstraint("agent_a_id < agent_b_id", name="ck_conversations_order"),
        Index(
            "uq_conversations_default_pair", "agent_a_id", "agent_b_id",
            unique=True, postgresql_where="is_default",
        ),
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    in_reply_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    sender_agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    recipient_agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_state: Mapped[str] = mapped_column(String(24), nullable=False, default="stored")
    delivery_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("sender_agent_id <> recipient_agent_id", name="ck_messages_not_self"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 16000", name="ck_messages_body_len"),
        Index("ix_messages_recipient_seq", "recipient_agent_id", "seq"),
        Index("ix_messages_recipient_pending", "recipient_agent_id", postgresql_where="acked_at IS NULL"),
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
